import copy
import operator
from typing import Any, Callable, Tuple

import torch
from torch.fx import Graph, GraphModule, Node
from torch.fx.subgraph_rewriter import replace_pattern_with_filters
import torch.nn.functional as F


# Example inputs for both `_conv2d_bn_pattern` and `_fused_qat_conv2d_bn_pattern`
_conv2d_bn_pattern_example_inputs = (
    torch.randn(1, 1, 3, 3),  # x
    torch.randn(1, 1, 1, 1),  # conv_weight
    torch.randn(1),           # conv_bias
    torch.randn(1),           # bn_weight
    torch.randn(1),           # bn_bias
    torch.randn(1),           # bn_running_mean
    torch.randn(1),           # bn_running_var
)

def _conv2d_bn_pattern(
    x: torch.Tensor,
    conv_weight: torch.Tensor,
    conv_bias: torch.Tensor,
    bn_weight: torch.Tensor,
    bn_bias: torch.Tensor,
    bn_running_mean: torch.Tensor,
    bn_running_var: torch.Tensor,
) -> torch.Tensor:
    x = F.conv2d(x, conv_weight, conv_bias)
    x = F.batch_norm(x, bn_running_mean, bn_running_var, bn_weight, bn_bias, training=True)
    return x

def _fused_qat_conv2d_bn_pattern(
    x: torch.Tensor,
    conv_weight: torch.Tensor,
    conv_bias: torch.Tensor,
    bn_weight: torch.Tensor,
    bn_bias: torch.Tensor,
    bn_running_mean: torch.Tensor,
    bn_running_var: torch.Tensor,
) -> torch.Tensor:
    """
    Approximated method to fuse conv and bn. It requires only one forward pass.
    conv_orig = conv / scale_factor where scale_factor = bn.weight / running_std.
    This is based on `nniqat.ConvBn2d._forward_approximate`.
    """
    # TODO: allow setting eps
    bn_eps = 1e-5
    running_std = torch.sqrt(bn_running_var + bn_eps)
    scale_factor = bn_weight / running_std
    weight_shape = [1] * len(conv_weight.shape)
    weight_shape[0] = -1
    bias_shape = [1] * len(conv_weight.shape)
    bias_shape[1] = -1
    scaled_weight = conv_weight * scale_factor.reshape(weight_shape)
    zero_bias = torch.zeros_like(conv_bias, dtype=x.dtype)
    x = F.conv2d(x, scaled_weight, zero_bias)
    x = x / scale_factor.reshape(bias_shape)
    x = x + conv_bias.reshape(bias_shape)
    x = F.batch_norm(x, bn_running_mean, bn_running_var, bn_weight, bn_bias, training=True, eps=bn_eps)
    return x

def _fused_qat_conv2d_bn_pattern_no_conv_bias(
    x: torch.Tensor,
    conv_weight: torch.Tensor,
    # Not used, only for matching convenience
    conv_bias: torch.Tensor,
    bn_weight: torch.Tensor,
    bn_bias: torch.Tensor,
    bn_running_mean: torch.Tensor,
    bn_running_var: torch.Tensor,
) -> torch.Tensor:
    """
    Same as `_fused_qat_conv2d_bn_pattern`, but handles the case with no conv bias.
    """
    # TODO: allow setting eps
    bn_eps = 1e-5
    running_std = torch.sqrt(bn_running_var + bn_eps)
    scale_factor = bn_weight / running_std
    weight_shape = [1] * len(conv_weight.shape)
    weight_shape[0] = -1
    bias_shape = [1] * len(conv_weight.shape)
    bias_shape[1] = -1
    scaled_weight = conv_weight * scale_factor.reshape(weight_shape)
    x = F.conv2d(x, scaled_weight, None)
    x = x / scale_factor.reshape(bias_shape)
    x = F.batch_norm(x, bn_running_mean, bn_running_var, bn_weight, bn_bias, training=True, eps=bn_eps)
    return x

def _get_aten_graph_module(
    pattern: Callable,
    example_inputs: Tuple[Any, ...],
) -> GraphModule:
    """
    Convert the pattern to an FX graph with decomposed aten ops.
    """
    # Avoid circular imports
    import torch._dynamo
    aten_pattern, _ = torch._dynamo.export(
        pattern,
        *copy.deepcopy(example_inputs),
        aten_graph=True,
        tracing_mode="real",
    )
    aten_pattern.graph.eliminate_dead_code()
    aten_pattern.recompile()
    return aten_pattern

def _has_conv_bias_filter(
    match: "InternalMatch",  # type: ignore[name-defined]
    original_graph: Graph,
    pattern_graph: Graph,
) -> bool:
    """
    Match filter for the subgraph rewriter that returns True if the conv node in
    the original graph has bias.
    """
    for _, n in match.nodes_map.items():
        if n.target == torch.ops.aten.convolution.default:
            return n.args[2] is not None
    raise ValueError("Could not find conv node in matched conv + bn pattern")

def _no_conv_bias_filter(
    match: "InternalMatch",  # type: ignore[name-defined]
    original_graph: Graph,
    pattern_graph: Graph,
) -> bool:
    """
    Match filter for the subgraph rewriter that returns True if the conv node in
    the original graph does NOT have bias.
    """
    return not _has_conv_bias_filter(match, original_graph, pattern_graph)

def _fuse_conv_bn_qat(m: GraphModule) -> GraphModule:
    """
    Given a graph of decomposed aten ops, replace the (conv + bn) pattern with
    the fused QAT subgraph equivalent. The input graph should already be annotated.
    The annotations in the original nodes will be preserved in the corresponding
    nodes in the new subgraph.

    Note: This also handles the (conv + bn + relu) pattern.
    """
    m.graph.eliminate_dead_code()
    m.recompile()
    example_inputs = _conv2d_bn_pattern_example_inputs
    match_pattern = _get_aten_graph_module(_conv2d_bn_pattern, example_inputs)

    # Step (1): Replace patterns with conv bias
    #
    # Here we do replacement separately for cases with and without conv bias, since
    # the replacement patterns for these two cases are substantially different.
    # TODO: use the public replace_pattern API once it also returns replacement nodes

    replacement_pattern_with_conv_bias = _get_aten_graph_module(
        _fused_qat_conv2d_bn_pattern,
        example_inputs,
    )
    replacements_with_conv_bias = replace_pattern_with_filters(
        m,
        match_pattern,
        replacement_pattern_with_conv_bias,
        match_filters=[_has_conv_bias_filter],
        ignore_literals=True,
    )
    m.recompile()

    # Step (2): Replace patterns without conv bias

    replacement_pattern_no_conv_bias = _get_aten_graph_module(
        _fused_qat_conv2d_bn_pattern_no_conv_bias,
        example_inputs,
    )
    replacements_no_conv_bias = replace_pattern_with_filters(
        m,
        match_pattern,
        replacement_pattern_no_conv_bias,
        match_filters=[_no_conv_bias_filter],
        ignore_literals=True,
    )
    m.recompile()

    # Step (3): Post processing
    #
    # Due to limited functionality in the subgraph rewriter, here we manually
    # update the replacement graph as follows:
    #
    #   (1) Copy over metadata from original subgraph. This ensures the stack traces
    #       and annotations are preserved in the new subgraph
    #
    #   (2) Copy over constant args for conv from the original subgraph
    #       TODO: do this for constant args for batchnorm as well
    #
    # In the future, we should try to push as much of this functionality into the
    # subgraph rewriter as possible, so we don't have to manually copy anything over.
    # For more detail, see https://github.com/pytorch/pytorch/issues/100419.

    for r in replacements_with_conv_bias + replacements_no_conv_bias:
        # Find replacement conv and bn nodes by climbing upwards from anchor node
        assert len(r.replacements) == 1, "expected only one replacement node"
        replacement_conv_node = None
        replacement_bn_node = None
        replacement_getitem_node = r.replacements[0]
        assert replacement_getitem_node.target == operator.getitem
        n = replacement_getitem_node
        while replacement_conv_node is None or replacement_bn_node is None:
            if n.target == torch.ops.aten.convolution.default:
                replacement_conv_node = n
            if n.target == torch.ops.aten._native_batch_norm_legit.default:
                replacement_bn_node = n
            assert isinstance(n.args[0], Node)
            n = n.args[0]

        # Copy over metadata for all three nodes in [conv - bn - getitem]
        # Also copy over constant args for conv
        for match_pattern_node, original_node in r.nodes_map.items():
            # bias can be None
            if original_node is None:
                continue
            if original_node.target == torch.ops.aten.convolution.default:
                replacement_conv_node.meta = original_node.meta
                # x, weight, bias, [stride, padding, dilation, transposed, output_padding, groups]
                replacement_conv_node.args = replacement_conv_node.args[:3] + original_node.args[3:]
            if original_node.target == torch.ops.aten._native_batch_norm_legit.default:
                replacement_bn_node.meta = original_node.meta
            if original_node.target == operator.getitem:
                replacement_getitem_node.meta = original_node.meta
    return m
