from typing import DefaultDict, Tuple, List, Dict, Any
import copy
import itertools
import warnings
from collections import defaultdict, namedtuple
import ast

import numpy as np
from sympy import Q
from dryvr_plus_plus.scene_verifier import agents

from dryvr_plus_plus.scene_verifier.agents.base_agent import BaseAgent
from dryvr_plus_plus.scene_verifier.automaton.guard import GuardExpressionAst
from dryvr_plus_plus.scene_verifier.automaton.reset import ResetExpression
from dryvr_plus_plus.scene_verifier.code_parser.parser import ControllerIR, unparse
from dryvr_plus_plus.scene_verifier.code_parser.pythonparser import Guard, Reset
from dryvr_plus_plus.scene_verifier.analysis.simulator import Simulator
from dryvr_plus_plus.scene_verifier.analysis.verifier import Verifier
from dryvr_plus_plus.scene_verifier.map.lane_map import LaneMap
from dryvr_plus_plus.scene_verifier.utils.utils import find, sample_rect
from dryvr_plus_plus.scene_verifier.analysis.analysis_tree_node import AnalysisTreeNode
from dryvr_plus_plus.scene_verifier.sensor.base_sensor import BaseSensor
from dryvr_plus_plus.scene_verifier.map.lane_map import LaneMap

EGO, OTHERS = "ego", "others"

class Scenario:
    def __init__(self):
        self.agent_dict = {}
        self.simulator = Simulator()
        self.verifier = Verifier()
        self.init_dict = {}
        self.init_mode_dict = {}
        self.static_dict = {}
        self.static_dict = {}
        self.map = LaneMap()
        self.sensor = BaseSensor()

    def set_sensor(self, sensor):
        self.sensor = sensor

    def set_map(self, lane_map:LaneMap):
        self.map = lane_map
        # Update the lane mode field in the agent
        for agent_id in self.agent_dict:
            agent = self.agent_dict[agent_id]
            self.update_agent_lane_mode(agent, lane_map)

    def add_agent(self, agent:BaseAgent):
        if self.map is not None:
            # Update the lane mode field in the agent
            self.update_agent_lane_mode(agent, self.map)
        self.agent_dict[agent.id] = agent

    # TODO-PARSER: update this function
    def update_agent_lane_mode(self, agent: BaseAgent, lane_map: LaneMap):
        for lane_id in lane_map.lane_dict:
            if 'LaneMode' in agent.controller.mode_defs and lane_id not in agent.controller.mode_defs['LaneMode'].modes:
                agent.controller.mode_defs['LaneMode'].modes.append(lane_id)
        # mode_vals = list(agent.controller.modes.values())
        # agent.controller.vertices = list(itertools.product(*mode_vals))
        # agent.controller.vertexStrings = [','.join(elem) for elem in agent.controller.vertices]

    def set_init(self, init_list, init_mode_list, static_list = []):
        assert len(init_list) == len(self.agent_dict)
        assert len(init_mode_list) == len(self.agent_dict)
        assert len(static_list) == len(self.agent_dict) or len(static_list) == 0
        for i,agent_id in enumerate(self.agent_dict.keys()):
            self.init_dict[agent_id] = copy.deepcopy(init_list[i])
            self.init_mode_dict[agent_id] = copy.deepcopy(init_mode_list[i])
            if static_list:
                self.static_dict[agent_id] = copy.deepcopy(static_list[i])
            else:
                self.static_dict[agent_id] = []

    def simulate_multi(self, time_horizon, num_sim):
        res_list = []
        for i in range(num_sim):
            trace = self.simulate(time_horizon)
            res_list.append(trace)
        return res_list
    
    def simulate(self, time_horizon, time_step):
        init_list = []
        init_mode_list = []
        static_list = []
        agent_list = []
        for agent_id in self.agent_dict:
            init_list.append(sample_rect(self.init_dict[agent_id]))
            init_mode_list.append(self.init_mode_dict[agent_id])
            static_list.append(self.static_dict[agent_id])
            agent_list.append(self.agent_dict[agent_id])
        print(init_list)
        return self.simulator.simulate(init_list, init_mode_list, static_list, agent_list, self, time_horizon, time_step, self.map)

    def verify(self, time_horizon, time_step):
        init_list = []
        init_mode_list = []
        agent_list = []
        for agent_id in self.agent_dict:
            init = self.init_dict[agent_id]
            tmp = np.array(init)
            if tmp.ndim < 2:
                init = [init, init]
            init_list.append(init)
            init_mode_list.append(self.init_mode_dict[agent_id])
            agent_list.append(self.agent_dict[agent_id])
        return self.verifier.compute_full_reachtube(init_list, init_mode_list, agent_list, self, time_horizon, time_step, self.map)

    def apply_reset(self, agent, reset_list, all_agent_state) -> Tuple[str, np.ndarray]:
        reset_expr = ResetExpression(reset_list)
        continuous_variable_dict, discrete_variable_dict, _ = self.sensor.sense(self, agent, all_agent_state, self.map)
        dest = reset_expr.get_dest(agent, all_agent_state[agent.id], discrete_variable_dict, self.map)
        rect = reset_expr.apply_reset_continuous(agent, continuous_variable_dict, self.map)
        return dest, rect

    def apply_cont_var_updater(self,cont_var_dict, updater):
        for variable in updater:
            for unrolled_variable, unrolled_variable_index in updater[variable]:
                cont_var_dict[unrolled_variable] = cont_var_dict[variable][unrolled_variable_index]

    # def apply_disc_var_updater(self,disc_var_dict, updater):
    #     for variable in updater:
    #         unrolled_variable, unrolled_variable_index = updater[variable]
    #         disc_var_dict[unrolled_variable] = disc_var_dict[variable][unrolled_variable_index]

    def get_transition_simulate_new(self, node:AnalysisTreeNode) -> Tuple[Dict[str, List[Tuple[float]]], float]:
        lane_map = self.map
        trace_length = len(list(node.trace.values())[0])

        # For each agent
        agent_guard_dict = defaultdict(list)

        for agent_id in node.agent:
            # Get guard
            agent:BaseAgent = self.agent_dict[agent_id]
            agent_mode = node.mode[agent_id]
            if agent.controller.controller == None:
                continue
            # TODO-PARSER: update how we get all next modes
            # The getNextModes function will return 
            paths = agent.controller.getNextModes()
            state_dict = {}
            for tmp in node.agent:
                state_dict[tmp] = (node.trace[tmp][0], node.mode[tmp], node.static[tmp])
            cont_var_dict_template, discrete_variable_dict, len_dict = self.sensor.sense(self, agent, state_dict, self.map)
            for guard_list, reset in paths:
                guard_expression = GuardExpressionAst(guard_list)

                # copy.deepcopy(guard_expression.ast_list[0].operand)
                # can_satisfy = guard_expression.fast_pre_process(discrete_variable_dict)
                continuous_variable_updater = guard_expression.parse_any_all_new(cont_var_dict_template, discrete_variable_dict, len_dict)
                agent_guard_dict[agent_id].append((guard_expression, continuous_variable_updater, copy.deepcopy(discrete_variable_dict), reset))

        transitions = defaultdict(list)
        # TODO: We can probably rewrite how guard hit are detected and resets are handled for simulation
        for idx in range(trace_length):
            satisfied_guard = []
            asserts = defaultdict(list)
            for agent_id in agent_guard_dict:
                agent:BaseAgent = self.agent_dict[agent_id]
                state_dict = {}
                for tmp in node.agent:
                    state_dict[tmp] = (node.trace[tmp][idx], node.mode[tmp], node.static[tmp])
                agent_state, agent_mode, agent_static = state_dict[agent_id]
                agent_state = agent_state[1:]
                continuous_variable_dict, orig_disc_vars, _ = self.sensor.sense(self, agent, state_dict, self.map)
                # Unsafety checking
                ego_ty_name = find(agent.controller.controller.args, lambda a: a[0] == EGO)[1]
                def pack_env(agent, cont, disc, map):
                    env = copy.deepcopy(cont)
                    env.update(disc)

                    state_ty = namedtuple(ego_ty_name, agent.controller.state_defs[ego_ty_name].all_vars())
                    packed: DefaultDict[str, Any] = defaultdict(dict)
                    for k, v in env.items():
                        k = k.split(".")
                        packed[k[0]][k[1]] = v
                    others_keys = list(packed[OTHERS].keys())
                    packed[OTHERS] = [state_ty(**{k: packed[OTHERS][k][i] for k in others_keys}) for i in range(len(packed[OTHERS][others_keys[0]]))]
                    packed[EGO] = state_ty(**packed[EGO])
                    map_var = find(agent.controller.controller.args, lambda a: "map" in a[0])
                    if map_var != None:
                        packed[map_var[0]] = map
                    packed: Dict[str, Any] = dict(packed.items())
                    packed.update(env)
                    return packed
                packed_env = pack_env(agent, continuous_variable_dict, orig_disc_vars, self.map)
                # assert agent.controller.controller.asserts
                def eval_expr(expr, env):
                    return eval(compile(ast.fix_missing_locations(ast.Expression(expr)), "", "eval"), env)
                for i, a in enumerate(agent.controller.controller.asserts):
                    pre_sat = all(eval_expr(p, packed_env) for p in a.pre)
                    if pre_sat:
                        cond_sat = eval_expr(a.cond, packed_env)
                        # print(a.cond, cond_sat)
                        if not cond_sat:
                            label = a.label if a.label != None else f"<assert {i}>"
                            del packed_env["__builtins__"]
                            print(f"assert hit for {agent_id}: \"{label}\" @ {packed_env}")
                            asserts[agent_id].append(label)
                if agent_id in asserts:
                    continue

                all_resets = defaultdict(list)
                for guard_expression, continuous_variable_updater, discrete_variable_dict, reset in agent_guard_dict[agent_id]:
                    new_cont_var_dict = copy.deepcopy(continuous_variable_dict)
                    # new_disc_var_dict = copy.deepcopy(discrete_variable_dict)
                    one_step_guard = copy.deepcopy(guard_expression).ast_list
                    self.apply_cont_var_updater(new_cont_var_dict, continuous_variable_updater)
                    # self.apply_disc_var_updater(new_disc_var_dict, discrete_variable_updater)
                    env = pack_env(agent, new_cont_var_dict, discrete_variable_dict, self.map)
                    if len(one_step_guard) == 0:
                        raise ValueError("empty guard")
                    if len(one_step_guard) == 1:
                        one_step_guard = one_step_guard[0]
                    elif len(one_step_guard) > 1:
                        one_step_guard = ast.BoolOp(ast.And(), one_step_guard)
                    guard_satisfied = eval_expr(one_step_guard, env)

                    # Collect all the hit guards for this agent at this time step
                    if guard_satisfied:
                        # If the guard can be satisfied, handle resets
                        reset_expr = ResetExpression(reset)
                        all_resets[reset_expr.var].append(reset_expr)
                
                iter_list = []
                for reset_var in all_resets:
                    iter_list.append(range(len(all_resets[reset_var])))
                pos_list = list(itertools.product(*iter_list))
                if len(pos_list)==1 and pos_list[0]==():
                    continue
                for i in range(len(pos_list)):
                    pos = pos_list[i]
                    next_init = copy.deepcopy(agent_state)
                    dest = copy.deepcopy(agent_mode)
                    possible_dest = [[elem] for elem in dest]
                    for j, reset_idx in enumerate(pos):
                        reset_variable = list(all_resets.keys())[j]
                        reset_expr:ResetExpression = all_resets[reset_variable][reset_idx]
                        res = eval_expr(reset_expr.val_ast , packed_env)
                        # print(ControllerIR.dump(reset_expr.expr), res)
                        if "mode" in reset_variable:
                            var_loc = agent.controller.state_defs[ego_ty_name].disc.index(reset_variable)
                            if not isinstance(res, list):
                                res = [res]
                            possible_dest[var_loc] = res
                        else:
                            var_loc = agent.controller.state_defs[ego_ty_name].cont.index(reset_variable)
                            next_init[var_loc] = res
                    all_dest = list(itertools.product(*possible_dest))
                    if not all_dest:
                        warnings.warn(f"Guard hit for mode {agent_mode} for agent {agent_id} without available next mode")
                        all_dest.append(None)
                    for dest in all_dest:
                        next_transition = (
                            agent_id, agent_mode, dest, next_init, 
                        )
                        satisfied_guard.append(next_transition)
            if len(asserts) > 0:
                return asserts, transitions, idx
            if len(satisfied_guard) > 0:
                for agent_idx, src_mode, dest_mode, next_init in satisfied_guard:
                    transitions[agent_idx].append((agent_idx, src_mode, dest_mode, next_init, idx))
                break
        return None, transitions, idx

    def get_transition_verify_new(self, node:AnalysisTreeNode):
        lane_map = self.map 
        possible_transitions = []
        
        agent_guard_dict = {}
        for agent_id in node.agent:
            agent:BaseAgent = self.agent_dict[agent_id]
            agent_mode = node.mode[agent_id]
            state_dict = {}
            for tmp in node.agent:
                state_dict[tmp] = (node.trace[tmp][0*2:0*2+2], node.mode[tmp], node.static[tmp])
            
            cont_var_dict_template, discrete_variable_dict, length_dict = self.sensor.sense(self, agent, state_dict, self.map)
            # TODO-PARSER: Get equivalent for this function
            paths = agent.controller.getNextModes()
            for path in paths:
                # Construct the guard expression
                guard_list = []
                reset_list = []
                for item in path:
                    if isinstance(item, Guard):
                        guard_list.append(item)
                    elif isinstance(item, Reset):
                        reset_list.append(item)
                guard_expression = GuardExpressionAst(guard_list)
                
                cont_var_updater = guard_expression.parse_any_all_new(cont_var_dict_template, discrete_variable_dict, length_dict)
                self.apply_cont_var_updater(cont_var_dict_template, cont_var_updater)
                guard_can_satisfied = guard_expression.evaluate_guard_disc(agent, discrete_variable_dict, cont_var_dict_template, self.map)
                if not guard_can_satisfied:
                    continue
                if agent_id not in agent_guard_dict:
                    agent_guard_dict[agent_id] = [(guard_expression, cont_var_updater, copy.deepcopy(discrete_variable_dict), reset_list)]
                else:
                    agent_guard_dict[agent_id].append((guard_expression, cont_var_updater, copy.deepcopy(discrete_variable_dict), reset_list))

        trace_length = int(len(list(node.trace.values())[0])/2)
        guard_hits = []
        guard_hit_bool = False
        for idx in range(0,trace_length):
            any_contained = False 
            hits = []
            state_dict = {}
            for tmp in node.agent:
                state_dict[tmp] = (node.trace[tmp][idx*2:idx*2+2], node.mode[tmp], node.static[tmp])
            
            for agent_id in agent_guard_dict:
                agent:BaseAgent = self.agent_dict[agent_id]
                agent_state, agent_mode = state_dict[agent_id]
                agent_state = agent_state[1:]
                continuous_variable_dict, _, _ = self.sensor.sense(self, agent, state_dict, self.map)
                for guard_expression, continuous_variable_updater, discrete_variable_dict, reset_list in agent_guard_dict[agent_id]:
                    new_cont_var_dict = copy.deepcopy(continuous_variable_dict)
                    one_step_guard:GuardExpressionAst = copy.deepcopy(guard_expression)

                    self.apply_cont_var_updater(new_cont_var_dict, continuous_variable_updater)
                    guard_can_satisfied = one_step_guard.evaluate_guard_hybrid(agent, discrete_variable_dict, new_cont_var_dict, self.map)
                    if not guard_can_satisfied:
                        continue
                    guard_satisfied, is_contained = one_step_guard.evaluate_guard_cont(agent, new_cont_var_dict, self.map)
                    any_contained = any_contained or is_contained
                    if guard_satisfied:
                        hits.append((agent_id, guard_list, reset_list))
            if hits != []:
                guard_hits.append((hits, state_dict, idx))
                guard_hit_bool = True 
            if hits == [] and guard_hit_bool:
                break 
            if any_contained:
                break

        reset_dict = {}
        reset_idx_dict = {}
        for hits, all_agent_state, hit_idx in guard_hits:
            for agent_id, guard_list, reset_list in hits:
                dest_list,reset_rect = self.apply_reset(node.agent[agent_id], reset_list, all_agent_state)
                if agent_id not in reset_dict:
                    reset_dict[agent_id] = {}
                    reset_idx_dict[agent_id] = {}
                if not dest_list:
                    warnings.warn(f"Guard hit for mode {node.mode[agent_id]} for agent {agent_id} without available next mode")
                    dest_list.append(None)
                for dest in dest_list:
                    if dest not in reset_dict[agent_id]:
                        reset_dict[agent_id][dest] = []
                        reset_idx_dict[agent_id][dest] = []
                    reset_dict[agent_id][dest].append(reset_rect)
                    reset_idx_dict[agent_id][dest].append(hit_idx)
            
        # Combine reset rects and construct transitions
        for agent in reset_dict:
            for dest in reset_dict[agent]:
                combined_rect = None 
                for rect in reset_dict[agent][dest]:
                    rect = np.array(rect)
                    if combined_rect is None:
                        combined_rect = rect 
                    else:
                        combined_rect[0,:] = np.minimum(combined_rect[0,:], rect[0,:])
                        combined_rect[1,:] = np.maximum(combined_rect[1,:], rect[1,:])
                combined_rect = combined_rect.tolist()
                min_idx = min(reset_idx_dict[agent][dest])
                max_idx = max(reset_idx_dict[agent][dest])
                transition = (agent, node.mode[agent], dest, combined_rect, (min_idx, max_idx))
                possible_transitions.append(transition)
        # Return result
        return possible_transitions
