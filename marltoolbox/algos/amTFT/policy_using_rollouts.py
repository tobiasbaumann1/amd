import numpy as np
from ray.rllib.evaluation import MultiAgentEpisode
from ray.rllib.utils.typing import TensorType
from typing import List, Union, Optional, Dict, Tuple

from marltoolbox.algos.amTFT.base_policy import amTFTPolicyBase, OWN_COOP_POLICY_IDX, OWN_SELFISH_POLICY_IDX, \
    OPP_SELFISH_POLICY_IDX, OPP_COOP_POLICY_IDX
from marltoolbox.utils import rollout


class amTFTRolloutsTorchPolicy(amTFTPolicyBase):

    def __init__(self, observation_space, action_space, config, **kwargs):
        super().__init__(observation_space, action_space, config, **kwargs)
        self._init_for_rollout(self.config)

    def _init_for_rollout(self, config):
        self.last_k = 1
        self.use_opponent_policies = False
        self.rollout_length = config["rollout_length"]
        self.n_rollout_replicas = config["n_rollout_replicas"]
        self.performing_rollouts = False
        self.overwrite_action = []
        self.own_policy_id = config["own_policy_id"]
        self.opp_policy_id = config["opp_policy_id"]
        self.n_steps_to_punish_opponent = 0

        # Don't support LSTM (at least because of action overwriting needed in the rollouts)
        if "model" in config.keys():
            if "use_lstm" in config["model"].keys():
                assert not config["model"]["use_lstm"]

    def compute_actions(
            self,
            obs_batch: Union[List[TensorType], TensorType],
            state_batches: Optional[List[TensorType]] = None,
            prev_action_batch: Union[List[TensorType], TensorType] = None,
            prev_reward_batch: Union[List[TensorType], TensorType] = None,
            info_batch: Optional[Dict[str, list]] = None,
            episodes: Optional[List["MultiAgentEpisode"]] = None,
            explore: Optional[bool] = None,
            timestep: Optional[int] = None,
            **kwargs) -> \
            Tuple[TensorType, List[TensorType], Dict[str, TensorType]]:

        # Option to overwrite action during internal rollouts
        if self.use_opponent_policies:
            if len(self.overwrite_action) > 0:
                actions, state_out, extra_fetches = self.overwrite_action.pop(0)
                if self.verbose > 1:
                    print("overwritten actions", actions, type(actions))
                return actions, state_out, extra_fetches

        return super().compute_actions(obs_batch, state_batches, prev_action_batch, prev_reward_batch,
                                       info_batch, episodes, explore, timestep, **kwargs)

    def _select_algo_to_use_in_eval(self):
        if not self.use_opponent_policies:
            if self.n_steps_to_punish == 0:
                self.active_algo_idx = OWN_COOP_POLICY_IDX
            elif self.n_steps_to_punish > 0:
                self.active_algo_idx = OWN_SELFISH_POLICY_IDX
                self.n_steps_to_punish -= 1
            else:
                raise ValueError("self.n_steps_to_punish can't be below zero")
        else:
            assert self.performing_rollouts
            if self.n_steps_to_punish_opponent == 0:
                self.active_algo_idx = OPP_COOP_POLICY_IDX
            elif self.n_steps_to_punish_opponent > 0:
                self.active_algo_idx = OPP_SELFISH_POLICY_IDX
                self.n_steps_to_punish_opponent -= 1
            else:
                raise ValueError("self.n_steps_to_punish_opp can't be below zero")

    def on_episode_step(self, opp_obs, last_obs, opp_action, worker, base_env, episode, env_index):
        if not self.performing_rollouts:
            super().on_episode_step(opp_obs, last_obs, opp_action, worker, base_env, episode, env_index)

    def _compute_debit(self, last_obs, opp_action, worker, base_env, episode, env_index, coop_opp_simulated_action):
        approximated_debit = self._compute_debit_using_rollouts(last_obs, opp_action, worker)
        return approximated_debit

    def _compute_debit_using_rollouts(self, last_obs, opp_action, worker):
        n_steps_to_punish, policy_map, policy_agent_mapping = self._prepare_to_perform_virtual_rollouts_in_env(worker)

        # Cooperative rollouts
        mean_total_reward_for_totally_coop_opp, _ = self._compute_opp_mean_total_reward(worker, policy_map,
                                                                                        policy_agent_mapping,
                                                                                        partially_coop=False,
                                                                                        opp_action=None,
                                                                                        last_obs=last_obs)
        # Cooperative rollouts with first action as the real one
        mean_total_reward_for_partially_coop_opp, _ = self._compute_opp_mean_total_reward(worker, policy_map,
                                                                                          policy_agent_mapping,
                                                                                          partially_coop=True,
                                                                                          opp_action=opp_action,
                                                                                          last_obs=last_obs)

        print("mean_total_reward_for_partially_coop_opp", mean_total_reward_for_partially_coop_opp)
        print("mean_total_reward_for_totally_coop_opp", mean_total_reward_for_totally_coop_opp)
        opp_reward_gain_from_picking_this_action = \
            mean_total_reward_for_partially_coop_opp - mean_total_reward_for_totally_coop_opp

        self._stop_performing_virtual_rollouts_in_env(n_steps_to_punish)

        return opp_reward_gain_from_picking_this_action

    def _prepare_to_perform_virtual_rollouts_in_env(self, worker):
        self.performing_rollouts = True
        self.use_opponent_policies = False
        n_steps_to_punish = self.n_steps_to_punish
        self.n_steps_to_punish = 0
        self.n_steps_to_punish_opponent = 0
        assert self.n_rollout_replicas // 2 > 0
        policy_map = {policy_id: self for policy_id in worker.policy_map.keys()}
        policy_agent_mapping = (lambda agent_id: self._switch_own_and_opp(agent_id))

        return n_steps_to_punish, policy_map, policy_agent_mapping

    def _stop_performing_virtual_rollouts_in_env(self, n_steps_to_punish):
        self.performing_rollouts = False
        self.use_opponent_policies = False
        self.n_steps_to_punish = n_steps_to_punish

    def _switch_own_and_opp(self, agent_id):
        if agent_id != self.own_policy_id:
            self.use_opponent_policies = True
        else:
            self.use_opponent_policies = False
        return self.own_policy_id

    def _compute_punishment_duration(self, opp_action, coop_opp_simulated_action, worker, last_obs):
        return self._compute_punishment_duration_from_rollouts(worker, last_obs)

    def _compute_punishment_duration_from_rollouts(self, worker, last_obs):
        # self.performing_rollouts = True
        # self.use_opponent_policies = False
        # n_steps_to_punish = self.n_steps_to_punish
        # assert self.n_rollout_replicas // 2 > 0
        # policy_map = {policy_id: self for policy_id in worker.policy_map.keys()}
        # policy_agent_mapping = (lambda agent_id: self._switch_own_and_opp(agent_id))
        n_steps_to_punish, policy_map, policy_agent_mapping = self._prepare_to_perform_virtual_rollouts_in_env(worker)

        self.k_opp_loss = {}
        k_to_explore = self.last_k
        self.debit_threshold_wt_multiplier = self.total_debit * self.punishment_multiplier

        continue_to_search_k = True
        while continue_to_search_k:
            k_to_explore, continue_to_search_k = self._search_duration_of_future_punishment(
                k_to_explore, worker, policy_map, policy_agent_mapping, last_obs)

        self._stop_performing_virtual_rollouts_in_env(n_steps_to_punish)
        self.last_k = k_to_explore

        print("k_opp_loss", self.k_opp_loss)
        print("k found", k_to_explore, "self.total_debit", self.total_debit,
              "debit_threshold_wt_multiplier", self.debit_threshold_wt_multiplier)
        return k_to_explore

    def _search_duration_of_future_punishment(self, k_to_explore, worker, policy_map, policy_agent_mapping, last_obs):

        _ = self._compute_opp_loss_for_one_k(k_to_explore, worker, policy_map, policy_agent_mapping, last_obs)
        n_steps_played = self._compute_opp_loss_for_one_k(k_to_explore - 1, worker, policy_map, policy_agent_mapping,
                                                          last_obs)

        continue_to_search_k = not self._is_k_found(k_to_explore)
        if continue_to_search_k:
            # If all the smallest k are already explored
            if (self.k_opp_loss[k_to_explore - 1] > self.debit_threshold_wt_multiplier and (k_to_explore - 1) <= 1):
                k_to_explore = 1
                continue_to_search_k = False
                return k_to_explore, continue_to_search_k

            # If there is not enough steps to be perform remaining in the episode
            # to compensate for the current total debit
            if k_to_explore >= n_steps_played and self.k_opp_loss[k_to_explore] < self.debit_threshold_wt_multiplier:
                print("n_steps_played", n_steps_played, "k_to_explore", k_to_explore)
                k_to_explore = max(self.k_opp_loss.keys())
                continue_to_search_k = False
                return k_to_explore, continue_to_search_k

            if self.k_opp_loss[k_to_explore] > self.debit_threshold_wt_multiplier:
                k_to_explore = min(self.k_opp_loss.keys())
            elif self.k_opp_loss[k_to_explore] < self.debit_threshold_wt_multiplier:
                k_to_explore = max(self.k_opp_loss.keys()) + 1

        return k_to_explore, continue_to_search_k

    def _compute_opp_loss_for_one_k(self, k_to_explore, worker, policy_map, policy_agent_mapping, last_obs):
        n_steps_played = 0
        if self._is_k_out_of_bound(k_to_explore):
            self.k_opp_loss[k_to_explore] = 0
        elif k_to_explore not in self.k_opp_loss.keys():
            opp_total_reward_loss, n_steps_played = self._compute_opp_total_reward_loss(k_to_explore, worker,
                                                                                        policy_map,
                                                                                        policy_agent_mapping,
                                                                                        last_obs=last_obs)
            self.k_opp_loss[k_to_explore] = opp_total_reward_loss
            if self.verbose > 0:
                print(f"k_to_explore {k_to_explore}: {opp_total_reward_loss}")

        return n_steps_played

    def _is_k_out_of_bound(self, k_to_explore):
        return k_to_explore <= 0 or k_to_explore > self.rollout_length

    def _is_k_found(self, k_to_explore):
        found_k = (self.k_opp_loss[k_to_explore] >= self.debit_threshold_wt_multiplier and
                   self.k_opp_loss[k_to_explore - 1] <= self.debit_threshold_wt_multiplier)
        return found_k

    def _compute_opp_total_reward_loss(self, k_to_explore, worker, policy_map, policy_agent_mapping, last_obs):
        # Cooperative rollouts
        coop_mean_total_reward, n_steps_played = self._compute_opp_mean_total_reward(worker, policy_map,
                                                                                     policy_agent_mapping,
                                                                                     partially_coop=False,
                                                                                     opp_action=None,
                                                                                     last_obs=last_obs)
        # Cooperative rollouts with first action as the real one
        partially_coop_mean_total_reward, _ = self._compute_opp_mean_total_reward(worker, policy_map,
                                                                                  policy_agent_mapping,
                                                                                  partially_coop=False,
                                                                                  opp_action=None, last_obs=last_obs,
                                                                                  k_to_explore=k_to_explore)

        opp_total_reward_loss = coop_mean_total_reward - partially_coop_mean_total_reward

        if self.verbose > 0:
            print(f"partially_coop_mean_total_reward {partially_coop_mean_total_reward}")
            print(f"coop_mean_total_reward {coop_mean_total_reward}")
            print(f"opp_total_reward_loss {opp_total_reward_loss}")

        return opp_total_reward_loss, n_steps_played

    def _compute_opp_mean_total_reward(self, worker, policy_map, policy_agent_mapping, partially_coop: bool,
                                       opp_action, last_obs, k_to_explore=0):
        opp_total_rewards = []
        for i in range(self.n_rollout_replicas // 2):
            self.n_steps_to_punish = k_to_explore
            self.n_steps_to_punish_opponent = k_to_explore
            if partially_coop:
                assert len(self.overwrite_action) == 0
                self.overwrite_action = [(np.array([opp_action]), [], {}), ]
            coop_rollout = rollout.internal_rollout(worker,
                                                    num_steps=self.rollout_length,
                                                    policy_map=policy_map,
                                                    last_obs=last_obs,
                                                    policy_agent_mapping=policy_agent_mapping,
                                                    reset_env_before=False,
                                                    num_episodes=1)
            assert coop_rollout._num_episodes == 1, f"coop_rollout._num_episodes {coop_rollout._num_episodes}"
            epi = coop_rollout._current_rollout
            opp_rewards = [step[3][self.opp_policy_id] for step in epi]
            # print("rewards", rewards)
            opp_total_reward = sum(opp_rewards)

            opp_total_rewards.append(opp_total_reward)
        # print("total_rewards", total_rewards)
        self.n_steps_to_punish = 0
        self.n_steps_to_punish_opponent = 0
        n_steps_played = len(epi)
        opp_mean_total_reward = sum(opp_total_rewards) / len(opp_total_rewards)
        return opp_mean_total_reward, n_steps_played
