import torch
from torch._subclasses import FakeTensor
from torch.ao.quantization.fx.prepare import (
    _get_arg_as_input_act_obs_or_fq,
    _get_output_act_obs_or_fq,
    _get_dtype_and_is_dynamic,
    _insert_obs_or_fq,
    _maybe_insert_output_observer_for_node,
    _is_observer_in_same_graph,
    _maybe_make_input_output_share_observers,
    _remove_output_observer,
    _maybe_insert_observers_before_graph_output,
    _save_state,
    _is_activation_post_process_node,
)
from torch.fx import (
    GraphModule,
    Node,
)
from torch.fx.node import Argument

from torch.ao.quantization import QConfigMapping
from torch.ao.quantization.qconfig import QConfigAny
from torch.ao.quantization.fx.custom_config import PrepareCustomConfig
from typing import Dict, Tuple, Union, Any
from torch.ao.quantization._pt2e.quantizer import (
    QuantizationAnnotation,
    EdgeOrNode,
)
from torch.ao.quantization import ObserverOrFakeQuantize

def _maybe_insert_input_observer_for_arg_or_kwarg(
    node: Union[Node, Any],
    arg: Argument,
    qconfig: QConfigAny,
    model: torch.nn.Module,
    named_modules: Dict[str, torch.nn.Module],
    obs_or_fq_map: Dict[EdgeOrNode, ObserverOrFakeQuantize],
) -> Argument:
    """
    Given a `node` and an `arg`, inserts an input observer between
    `node` and `arg` if necessary.
    """
    # for ops such as torch.cat([x0, x1]),
    # traverse through the list
    if isinstance(arg, (list, tuple)):
        new_arg_to_return = []
        for inner_arg in arg:
            new_inner_arg = _maybe_insert_input_observer_for_arg_or_kwarg(
                node, inner_arg, qconfig, model, named_modules, obs_or_fq_map
            )
            new_arg_to_return.append(new_inner_arg)
        return type(arg)(new_arg_to_return)

    if not isinstance(arg, Node):
        return arg
    assert isinstance(arg, Node)
    # default (no observer)
    new_arg = arg

    quantization_annotation = node.meta.get("quantization_annotation", QuantizationAnnotation())
    reuse_input_obs_or_fq = quantization_annotation._reuse_input_obs_or_fq
    arg_as_input_act_obs_or_fq = _get_arg_as_input_act_obs_or_fq(arg, node, named_modules, obs_or_fq_map)
    arg_as_input_target_dtype, arg_as_input_target_is_dynamic = _get_dtype_and_is_dynamic(arg_as_input_act_obs_or_fq)

    arg_as_output_act_obs_or_fq = _get_output_act_obs_or_fq(arg, named_modules, obs_or_fq_map)
    arg_as_output_target_dtype, arg_as_output_target_is_dynamic = _get_dtype_and_is_dynamic(arg_as_output_act_obs_or_fq)

    if arg_as_input_target_is_dynamic or arg_as_input_target_dtype not in [torch.float, None]:
        if arg_as_input_target_dtype == arg_as_output_target_dtype and \
           arg_as_input_target_is_dynamic == arg_as_output_target_is_dynamic:
            assert _is_activation_post_process_node(arg, named_modules)
            assert arg_as_input_act_obs_or_fq is not None
            observed_arg = arg.args[0]
            assert isinstance(observed_arg, Node), "expect observed argument to be a Node, but got: {}".format(type(observed_arg))
            assert observed_arg in obs_or_fq_map, \
                "can't refer to a node that does not have observer/fake_quant inserted yet: {}".format(observed_arg)
            arg_as_input_act_obs_or_fq = obs_or_fq_map[observed_arg]
            new_arg = arg
            obs_or_fq_map[(observed_arg, node)] = arg_as_input_act_obs_or_fq
        else:
            assert arg_as_input_act_obs_or_fq is not None
            new_obs_node = _insert_obs_or_fq(
                arg, arg_as_input_act_obs_or_fq, model, named_modules, model.graph)  # type: ignore[arg-type]
            new_arg = new_obs_node
            obs_or_fq_map[(arg, node)] = arg_as_input_act_obs_or_fq

    return new_arg

def _maybe_insert_input_observers_for_node(
    node: Node,
    qconfig: QConfigAny,
    model: torch.nn.Module,
    named_modules: Dict[str, torch.nn.Module],
    obs_or_fq_map: Dict[EdgeOrNode, ObserverOrFakeQuantize],
) -> None:
    """
    If needed, inserts observers to the input args and kwargs of `node`.
    Note: modifies `node` inplace.

    For example, if cur_node needs an observer after prev_node, we change from

      prev_node -> cur_node

    To

      prev_node -> obs -> cur_node

    """
    # Look through every input arg.  If that arg's target dtype does not
    # match the current node's target dtype, insert an observer.
    new_args = []
    for arg in node.args:
        new_arg = _maybe_insert_input_observer_for_arg_or_kwarg(
            node, arg, qconfig, model, named_modules, obs_or_fq_map
        )
        new_args.append(new_arg)

    assert len(node.kwargs) == 0, " expecting kwargs for aten op IR to be empty"

    # assign the new args to the node, inplace
    node.args = tuple(new_args)

def _maybe_insert_input_and_output_observers_for_node(
    node: Node,
    model: torch.fx.GraphModule,
    obs_or_fq_map: Dict[EdgeOrNode, ObserverOrFakeQuantize],
):
    this_node_dtype_info = node.meta["quantization_annotation"] if "quantization_annotation" in node.meta else None
    if "val" in node.meta:
        output_is_a_tensor = (
            this_node_dtype_info is not None and
            isinstance(node.meta["val"], FakeTensor)
        )
    else:
        output_is_a_tensor = this_node_dtype_info is not None

    skip_inserting_input_and_output_observers = (
        this_node_dtype_info is None
    )

    if skip_inserting_input_and_output_observers:
        return

    named_modules = dict(model.named_modules(remove_duplicate=False))

    _maybe_insert_input_observers_for_node(
        node,
        None,  # qconfig
        model,
        named_modules,
        obs_or_fq_map,
    )

    skip_inserting_output_observers = (
        not output_is_a_tensor
    )

    if skip_inserting_output_observers:
        return

    # this returns the new observer node if it was needed
    maybe_output_obs_node = _maybe_insert_output_observer_for_node(node, model, named_modules, model.graph, obs_or_fq_map)

    if maybe_output_obs_node is None:
        return
    # Update users of original node to use the output observer
    # instead. For example, change
    #
    #           next_node
    #          /
    #   cur_node -> obs
    #
    # to
    #
    #                 next_node
    #                 /
    #   cur_node -> obs
    #
    # We need to save orig users before updating uses because
    # the list of users will change as we update uses
    orig_users = list(node.users.keys())
    for user_node in orig_users:
        if user_node is maybe_output_obs_node:
            continue
        user_node.replace_input_with(node, maybe_output_obs_node)
    _is_observer_in_same_graph_ = _is_observer_in_same_graph(node, named_modules, obs_or_fq_map)

    input_output_share_observers = node.meta["quantization_annotation"]._input_output_share_observers
    reuse_input_obs_or_fq = node.meta["quantization_annotation"]._reuse_input_obs_or_fq
    if (input_output_share_observers and _is_observer_in_same_graph_) or \
       reuse_input_obs_or_fq:
        if not _maybe_make_input_output_share_observers(node, model, named_modules):
            _remove_output_observer(node, model, named_modules)

def prepare(
    model: GraphModule,
    node_name_to_scope: Dict[str, Tuple[str, type]],
    is_qat: bool,
) -> GraphModule:
    # Since we are mutating the graph as we go, we iterate over the original
    # nodes before observer insertion, instead of model.graph.nodes.
    nodes_before_observation = list(model.graph.nodes)
    obs_or_fq_map: Dict[EdgeOrNode, ObserverOrFakeQuantize] = {}

    for node in nodes_before_observation:

        if node.op in (
            "placeholder",
            "call_module",
            "call_method",
            "call_function",
            "get_attr",
        ):
            _maybe_insert_input_and_output_observers_for_node(node, model, obs_or_fq_map)
        elif node.op == "output":
            named_modules = dict(model.named_modules(remove_duplicate=False))
            _maybe_insert_observers_before_graph_output(node, model, named_modules, model.graph, obs_or_fq_map)

    model = GraphModule(model, model.graph)

    _save_state(
        model,
        {},  # node_name_to_qconfig
        node_name_to_scope,
        PrepareCustomConfig(),
        {},  # equalization_node_name_to_qconfig
        QConfigMapping(),
        is_qat,
        set()  # observed_node_names
    )
    return model
