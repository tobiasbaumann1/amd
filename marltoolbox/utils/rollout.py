import copy

import collections
from gym import wrappers as gym_wrappers
from ray.rllib.env import MultiAgentEnv
from ray.rllib.env.base_env import _DUMMY_AGENT_ID
from ray.rllib.policy.sample_batch import DEFAULT_POLICY_ID
from ray.rllib.rollout import DefaultMapping, default_policy_agent_mapping, RolloutSaver #, keep_going
from ray.rllib.utils.framework import TensorStructType
from ray.rllib.utils.spaces.space_utils import flatten_to_single_ndarray
from ray.rllib.utils.typing import EnvInfoDict, PolicyID
from typing import List


def keep_going(steps, num_steps, episodes, num_episodes):
    """Determine whether we've collected enough data"""
    if num_episodes and num_steps:
        return episodes < num_episodes and steps < num_steps
    # if num_episodes is set, this overrides num_steps
    if num_episodes:
        return episodes < num_episodes
    # if num_steps is set, continue until we reach the limit
    if num_steps:
        return steps < num_steps
    # otherwise keep going forever
    return True

class RolloutManager(RolloutSaver):
    """Utility class for storing rollouts.

    Currently supports two behaviours: the original, which
    simply dumps everything to a pickle file once complete,
    and a mode which stores each rollout as an entry in a Python
    shelf db file. The latter mode is more robust to memory problems
    or crashes part-way through the rollout generation. Each rollout
    is stored with a key based on the episode number (0-indexed),
    and the number of episodes is stored with the key "num_episodes",
    so to load the shelf file, use something like:

    with shelve.open('rollouts.pkl') as rollouts:
       for episode_index in range(rollouts["num_episodes"]):
          rollout = rollouts[str(episode_index)]

    If outfile is None, this class does nothing.
    """
    def end_rollout(self):
        if self._use_shelve:
            # Save this episode as a new entry in the shelf database,
            # using the episode number as the key.
            self._shelf[str(self._num_episodes)] = self._current_rollout
        else:
            # Append this rollout to our list, to save laer.
            self._rollouts.append(self._current_rollout)
        self._num_episodes += 1
        if self._update_file:
            self._update_file.seek(0)
            self._update_file.write(self._get_progress() + "\n")
            self._update_file.flush()

    def append_step(self, obs, action, next_obs, reward, done, info):
        """Add a step to the current rollout, if we are saving them"""
        if self._save_info:
            self._current_rollout.append(
                [obs, action, next_obs, reward, done, info])
        else:
            self._current_rollout.append(
                [obs, action, next_obs, reward, done])
        self._total_steps += 1


# Modified from ray.rllib.rollout.py
def internal_rollout(worker,
                     num_steps,
                     policy_map=None,
                     policy_agent_mapping=None,
                     reset_env_before=True,
                     num_episodes=0,
                     last_obs=None,
                     saver=None,
                     no_render=True,
                     video_dir=None,
                     seed=None
                     ):
    assert num_steps is not None or num_episodes is not None

    if saver is None:
        saver = RolloutManager()

    env = copy.deepcopy(worker.env)
    if hasattr(env, "seed"):
        env.seed(seed)

    multiagent = isinstance(env, MultiAgentEnv)

    if policy_agent_mapping is None:
        if worker.multiagent:
            # policy_agent_mapping = agent.config["multiagent"]["policy_mapping_fn"]
            policy_agent_mapping = worker.policy_config["multiagent"]["policy_mapping_fn"]
        else:
            policy_agent_mapping = default_policy_agent_mapping

    if policy_map is None:
        policy_map = worker.policy_map
    state_init = {p: m.get_initial_state() for p, m in policy_map.items()}
    use_lstm = {p: len(s) > 0 for p, s in state_init.items()}
    action_init = {
        p: flatten_to_single_ndarray(m.action_space.sample())
        for p, m in policy_map.items()
    }

    # If monitoring has been requested, manually wrap our environment with a
    # gym monitor, which is set to record every episode.
    if video_dir:
        env = gym_wrappers.Monitor(
            env=env,
            directory=video_dir,
            video_callable=lambda x: True,
            force=True)

    random_policy_id = list(policy_map.keys())[0]
    virtual_global_timestep = worker.get_policy(random_policy_id).global_timestep

    steps = 0
    episodes = 0
    while keep_going(steps, num_steps, episodes, num_episodes):
        mapping_cache = {}  # in case policy_agent_mapping is stochastic
        saver.begin_rollout()
        if reset_env_before or episodes > 0:
            obs = env.reset()
        else:
            obs = last_obs
        agent_states = DefaultMapping(
            lambda agent_id: state_init[mapping_cache[agent_id]])
        prev_actions = DefaultMapping(
            lambda agent_id: action_init[mapping_cache[agent_id]])
        prev_rewards = collections.defaultdict(lambda: 0.)
        done = False
        reward_total = 0.0
        while not done and keep_going(steps, num_steps, episodes,
                                      num_episodes):
            multi_obs = obs if multiagent else {_DUMMY_AGENT_ID: obs}
            action_dict = {}
            virtual_global_timestep += 1
            for agent_id, a_obs in multi_obs.items():
                if a_obs is not None:
                    policy_id = mapping_cache.setdefault(
                        agent_id, policy_agent_mapping(agent_id))
                    p_use_lstm = use_lstm[policy_id]
                    if p_use_lstm:
                        a_action, p_state, _ = worker_compute_action(worker,
                                                                     timestep=virtual_global_timestep,
                                                                     observation=a_obs,
                                                                     state=agent_states[agent_id],
                                                                     prev_action=prev_actions[agent_id],
                                                                     prev_reward=prev_rewards[agent_id],
                                                                     policy_id=policy_id)
                        agent_states[agent_id] = p_state
                    else:
                        a_action = worker_compute_action(worker, virtual_global_timestep,
                                                         observation=a_obs,
                                                         prev_action=prev_actions[agent_id],
                                                         prev_reward=prev_rewards[agent_id],
                                                         policy_id=policy_id)
                    a_action = flatten_to_single_ndarray(a_action)
                    action_dict[agent_id] = a_action
                    prev_actions[agent_id] = a_action

                    # print(policy_id, a_action)
            action = action_dict

            action = action if multiagent else action[_DUMMY_AGENT_ID]
            next_obs, reward, done, info = env.step(action)
            if multiagent:
                for agent_id, r in reward.items():
                    prev_rewards[agent_id] = r
            else:
                prev_rewards[_DUMMY_AGENT_ID] = reward

            if multiagent:
                done = done["__all__"]
                reward_total += sum(
                    r for r in reward.values() if r is not None)
            else:
                reward_total += reward
            if not no_render:
                env.render()
            saver.append_step(obs, action, next_obs, reward, done, info)
            steps += 1
            obs = next_obs
        saver.end_rollout()
        # print("Episode #{}: reward: {}".format(episodes, reward_total))
        if done:
            episodes += 1
    return saver


# Modified from ray.rllib.agent.trainer.py
def worker_compute_action(worker, timestep,
                          observation: TensorStructType,
                          state: List[TensorStructType] = None,
                          prev_action: TensorStructType = None,
                          prev_reward: float = None,
                          info: EnvInfoDict = None,
                          policy_id: PolicyID = DEFAULT_POLICY_ID,
                          full_fetch: bool = False,
                          explore: bool = None) -> TensorStructType:
    """Computes an action for the specified policy on the local Worker.

    Note that you can also access the policy object through
    self.get_policy(policy_id) and call compute_actions() on it directly.

    Arguments:
        observation (TensorStructType): observation from the environment.
        state (List[TensorStructType]): RNN hidden state, if any. If state
            is not None, then all of compute_single_action(...) is returned
            (computed action, rnn state(s), logits dictionary).
            Otherwise compute_single_action(...)[0] is returned
            (computed action).
        prev_action (TensorStructType): Previous action value, if any.
        prev_reward (float): Previous reward, if any.
        info (EnvInfoDict): info object, if any
        policy_id (PolicyID): Policy to query (only applies to
            multi-agent).
        full_fetch (bool): Whether to return extra action fetch results.
            This is always set to True if RNN state is specified.
        explore (bool): Whether to pick an exploitation or exploration
            action (default: None -> use self.config["explore"]).

    Returns:
        any: The computed action if full_fetch=False, or
        tuple: The full output of policy.compute_actions() if
            full_fetch=True or we have an RNN-based Policy.
    """
    if state is None:
        state = []
    preprocessed = worker.preprocessors[
        policy_id].transform(observation)
    filtered_obs = worker.filters[policy_id](
        preprocessed, update=False)

    result = worker.get_policy(policy_id).compute_single_action(
        filtered_obs,
        state,
        prev_action,
        prev_reward,
        info,
        clip_actions=worker.policy_config["clip_actions"],
        explore=explore,
        timestep=timestep)

    if state or full_fetch:
        return result
    else:
        return result[0]  # backwards compatibility
