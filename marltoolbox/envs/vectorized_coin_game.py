##########
# Code modified from: https://github.com/julianstastny/openspiel-social-dilemmas/blob/master/games/coin_game_gym.py
##########
import copy
from collections import Iterable

import gym
import numpy as np
from gym.spaces import Discrete
from gym.utils import seeding
from numba import jit, prange
from numba.typed import List
from ray.rllib.env.multi_agent_env import MultiAgentEnv

from envs.utils.interfaces import InfoAccumulationInterface

@jit(nopython=True)
def _same_pos(x, y):
    return (x == y).all()


@jit(nopython=True)
def move_players(batch_size, actions, red_pos, blue_pos, moves, grid_size):
    for j in prange(batch_size):
        red_pos[j] = \
            (red_pos[j] + moves[actions[j, 0]]) % grid_size
        blue_pos[j] = \
            (blue_pos[j] + moves[actions[j, 1]]) % grid_size
    return red_pos, blue_pos


@jit(nopython=True)
def compute_reward(batch_size, red_pos, blue_pos, coin_pos, red_coin, asymmetric):
    # Changing something here imply changing something in the analysis of the rewards
    reward_red = np.zeros(batch_size)
    reward_blue = np.zeros(batch_size)
    generate = np.zeros(batch_size, dtype=np.bool_)
    red_pick_any, red_pick_red, blue_pick_any, blue_pick_blue = False, False, False, False
    for i in prange(batch_size):
        if red_coin[i]:
            if _same_pos(red_pos[i], coin_pos[i]):
                generate[i] = True
                reward_red[i] += 1
                if asymmetric:
                    reward_red[i] += 3
                red_pick_any = True
                red_pick_red = True
            if _same_pos(blue_pos[i], coin_pos[i]):
                generate[i] = True
                reward_red[i] += -2
                reward_blue[i] += 1
                blue_pick_any = True
        else:
            if _same_pos(red_pos[i], coin_pos[i]):
                generate[i] = True
                reward_red[i] += 1
                reward_blue[i] += -2
                if asymmetric:
                    reward_red[i] += 3
                red_pick_any = True
            if _same_pos(blue_pos[i], coin_pos[i]):
                generate[i] = True
                reward_blue[i] += 1
                blue_pick_any = True
                blue_pick_blue = True
    reward = [reward_red, reward_blue]
    return reward, generate, red_pick_any, red_pick_red, blue_pick_any, blue_pick_blue


@jit(nopython=True)
def _flatten_index(pos, grid_size):
    y_pos, x_pos = pos
    idx = grid_size * y_pos
    idx += x_pos
    return idx


@jit(nopython=True)
def _unflatten_index(pos, grid_size):
    x_idx = pos % grid_size
    y_idx = pos // grid_size
    return np.array([y_idx, x_idx])


@jit(nopython=True)
def place_coin(red_pos_i, blue_pos_i, grid_size):
    red_pos_flat = _flatten_index(red_pos_i, grid_size)
    blue_pos_flat = _flatten_index(blue_pos_i, grid_size)
    possible_coin_pos = np.array([x for x in range(9) if ((x != blue_pos_flat) and (x != red_pos_flat))])
    flat_coin_pos = np.random.choice(possible_coin_pos)
    return _unflatten_index(flat_coin_pos, grid_size)


@jit(nopython=True)
def generate_coin(batch_size, generate, red_coin, red_pos, blue_pos, coin_pos, grid_size):
    red_coin[generate] = 1 - red_coin[generate]
    for i in prange(batch_size):
        if generate[i]:
            coin_pos[i] = place_coin(red_pos[i], blue_pos[i], grid_size)
    return coin_pos


@jit(nopython=True)
def generate_state(batch_size, red_pos, blue_pos, coin_pos, red_coin,
                   add_position_in_epi, step_count, max_steps, grid_size):
    if add_position_in_epi:
        state = np.zeros((batch_size, grid_size, grid_size, 5))
    else:
        state = np.zeros((batch_size, grid_size, grid_size, 4))
    for i in prange(batch_size):
        state[i, red_pos[i][0], red_pos[i][1], 0] = 1
        state[i, blue_pos[i][0], blue_pos[i][1], 1] = 1
        if red_coin[i]:
            state[i, coin_pos[i][0], coin_pos[i][1], 2] = 1
        else:
            state[i, coin_pos[i][0], coin_pos[i][1], 3] = 1
    if add_position_in_epi:
        state[:, :, :, 4] = step_count / max_steps
    return state


@jit(nopython=True)
def batch_step_with_numba_optimization(actions, batch_size, red_pos, blue_pos, coin_pos, red_coin, moves,
         grid_size: int, asymmetric: bool, add_position_in_epi: bool, step_count: int, max_steps: int):
    red_pos, blue_pos = move_players(batch_size, actions, red_pos, blue_pos, moves, grid_size)
    reward, generate, red_pick_any, red_pick_red, blue_pick_any, blue_pick_blue = compute_reward(
        batch_size, red_pos, blue_pos, coin_pos, red_coin, asymmetric)
    coin_pos = generate_coin(batch_size, generate, red_coin, red_pos, blue_pos, coin_pos, grid_size)
    state = generate_state(batch_size, red_pos, blue_pos, coin_pos, red_coin, add_position_in_epi, step_count,
                           max_steps, grid_size)
    return red_pos, blue_pos, reward, coin_pos, state, red_coin, red_pick_any, red_pick_red, blue_pick_any, blue_pick_blue


class CoinGame(InfoAccumulationInterface, MultiAgentEnv, gym.Env):
    """
    Vectorized Coin Game environment.
    Note: slightly deviates from the Gym API.
    """
    NAME = "CoinGame"
    NUM_AGENTS = 2
    NUM_ACTIONS = 4
    ACTION_SPACE = Discrete(NUM_ACTIONS)
    OBSERVATION_SPACE = None
    MOVES = List([
        np.array([0, 1]),
        np.array([0, -1]),
        np.array([1, 0]),
        np.array([-1, 0]),
    ])

    def __init__(self, config={}):

        if "players_ids" in config:
            assert isinstance(config["players_ids"], Iterable) and len(config["players_ids"]) == self.NUM_AGENTS

        self.players_ids = config.get("players_ids", ["player_red", "player_blue"])
        self.player_red_id, self.player_blue_id = self.players_ids
        self.batch_size = config.get("batch_size", 1)
        self.force_vectorized = config.get("force_vectorize", False)
        self.max_steps = config.get("max_steps", 20)
        self.grid_size = config.get("grid_size", 3)
        assert self.grid_size == 3, "hardoced in the generate_state function"
        self.get_additional_info = config.get("get_additional_info", True)
        self.asymmetric = config.get("asymmetric", False)
        self.add_position_in_epi = config.get("add_position_in_epi", False)
        self.flatten_obs = config.get("flatten_obs", False)
        self.n_features = self.grid_size ** 2 * (2 * self.NUM_AGENTS + int(self.add_position_in_epi))

        # if self.flatten_obs:
        #     self.OBSERVATION_SPACE = gym.spaces.Box(
        #         low=0,
        #         high=1,
        #         shape=(self.n_features,),
        #         dtype='uint8'
        #     )
        # else:
        self.OBSERVATION_SPACE = gym.spaces.Box(
            low=0,
            high=1,
            shape=(self.grid_size, self.grid_size, 4 + int(self.add_position_in_epi)),
            dtype='uint8'
        )

        self.step_count = None

        if self.get_additional_info:
            self._init_info()

    def reset(self):
        self.step_count = 0
        if self.get_additional_info:
            self._reset_info()

        self.red_coin = np.random.randint(2, size=self.batch_size)
        # Agent and coin positions
        self.red_pos = np.random.randint(
            self.grid_size, size=(self.batch_size, 2))
        self.blue_pos = np.random.randint(
            self.grid_size, size=(self.batch_size, 2))
        self.coin_pos = np.zeros((self.batch_size, 2), dtype=np.int8)
        for i in range(self.batch_size):
            # Make sure players don't overlap
            while _same_pos(self.red_pos[i], self.blue_pos[i]):
                self.blue_pos[i] = np.random.randint(self.grid_size, size=2)

        generate = np.ones(self.batch_size, dtype=bool)
        self.coin_pos = generate_coin(
            self.batch_size, generate, self.red_coin, self.red_pos, self.blue_pos, self.coin_pos, self.grid_size)
        state = generate_state(self.batch_size, self.red_pos, self.blue_pos, self.coin_pos,
                               self.red_coin, self.add_position_in_epi, self.step_count,
                               self.max_steps, self.grid_size)

        # if self.flatten_obs:
        #     state = np.reshape(state, (state.shape[0], -1))

        # Unvectorize if batch_size == 1 (do not return batch of states)
        if self.batch_size == 1 and not self.force_vectorized:
            state = state[0, ...]

        return {
            self.player_red_id: state,
            self.player_blue_id: state
        }


    def step(self, actions: Iterable):
        """
        :param actions: Dict containing both actions for player_1 and player_2
        :return: observations, rewards, done, info
        """
        actions = self._from_RLLib_API_to_list(actions)
        self.step_count += 1

        (self.red_pos, self.blue_pos, rewards, self.coin_pos, observation, self.red_coin,
         red_pick_any, red_pick_red, blue_pick_any, blue_pick_blue) = batch_step_with_numba_optimization(
            actions, self.batch_size, self.red_pos, self.blue_pos, self.coin_pos, self.red_coin, self.MOVES,
            self.grid_size, self.asymmetric, self.add_position_in_epi, self.step_count, self.max_steps)

        if self.get_additional_info:
            self._accumulate_info(red_pick_any, red_pick_red, blue_pick_any, blue_pick_blue)

        # if self.flatten_obs:
        #     observation = np.reshape(observation, (observation.shape[0], -1))

        # Unvectorize if batch_size == 1 (do not return batch of states and rewards)
        if self.batch_size == 1 and not self.force_vectorized:
            observation = observation[0, ...]
            rewards[0], rewards[1] = rewards[0][0], rewards[1][0]

        return self._to_RLLib_API(observation, rewards)

    def seed(self, seed=None):
        """Seed the PRNG of this space. """
        self.np_random, seed = seeding.np_random(seed)
        return [seed]

    def _from_RLLib_API_to_list(self, actions):
        """
        Format actions from dict of players to list of lists
        """
        ac_red, ac_blue = actions[self.player_red_id], actions[self.player_blue_id]
        if not isinstance(ac_red, Iterable):
            ac_red, ac_blue = [ac_red], [ac_blue]
        actions = [ac_red, ac_blue]
        actions = np.array(actions).T
        return actions

    def _to_RLLib_API(self, observation, rewards):
        observations = {
            self.player_red_id: observation,
            self.player_blue_id: observation,
        }
        rewards = {
            self.player_red_id: rewards[0],
            self.player_blue_id: rewards[1],
        }

        epi_is_done = (self.step_count == self.max_steps)
        done = {
            self.player_red_id: epi_is_done,
            self.player_blue_id: epi_is_done,
            "__all__": epi_is_done,
        }

        if epi_is_done and self.get_additional_info:
            info_red, info_blue = self._get_episode_info()
            info = {
                self.player_red_id: info_red,
                self.player_blue_id: info_blue,
            }
        else:
            info = {}

        return observations, rewards, done, info

    def _accumulate_info(self, red_pick_any, red_pick_red, blue_pick_any, blue_pick_blue):
        self.red_pick.append(red_pick_any)
        self.red_pick_own.append(red_pick_red)
        self.blue_pick.append(blue_pick_any)
        self.blue_pick_own.append(blue_pick_blue)

    def _get_episode_info(self):
        """
        Output the following information:
        pick_speed is the fraction of steps during which the player picked a coin
        pick_own_color is the fraction of coins picked by the player which have the same color as the player
        """
        red_info, blue_info = {}, {}

        if len(self.red_pick) > 0:
            red_pick = sum(self.red_pick)
            red_info["pick_speed"] = red_pick / len(self.red_pick)
            if red_pick > 0:
                red_info["pick_own_color"] = sum(self.red_pick_own) / red_pick

        if len(self.blue_pick) > 0:
            blue_pick = sum(self.blue_pick)
            blue_info["pick_speed"] = blue_pick / len(self.blue_pick)
            if blue_pick > 0:
                blue_info["pick_own_color"] = sum(self.blue_pick_own) / blue_pick

        return red_info, blue_info

    def _reset_info(self):
        self.red_pick.clear()
        self.red_pick_own.clear()
        self.blue_pick.clear()
        self.blue_pick_own.clear()

    def _init_info(self):
        self.red_pick = []
        self.red_pick_own = []
        self.blue_pick = []
        self.blue_pick_own = []

    # def _compute_rewards(self, state, actions):
    #     """Works only for flatten state"""
    #     assert self.flatten_obs
    #     assert self.batch_size == 1
    #     save_env_state = self._get_env_state()
    #     self._state_to_env_state(state)
    #     actions = self._preprocess_actions(actions)
    #
    #     red_pos, blue_pos = move_players(self.batch_size, actions, self.red_pos, self.blue_pos,
    #                                      self.MOVES, self.grid_size)
    #     reward, generate = compute_reward(self.batch_size, red_pos, blue_pos,
    #                                       self.coin_pos, self.red_coin, self.asymmetric)
    #
    #     self._set_env_state(save_env_state)
    #
    #     if self.batch_size == 1 and not self.force_vectorized:
    #         reward[0], reward[1] = reward[0][0], reward[1][0]
    #
    #     reward = {
    #         self.player_red_id: reward[0],
    #         self.player_blue_id: reward[1],
    #     }
    #     return reward

    # def _state_to_env_state(self, state):
    #     if self.flatten_obs:
    #         assert state.shape[0] == 1
    #         state = state[0, ...]
    #         # Only working for 2 players
    #         red_pos = np.nonzero(state[0::4])[0]
    #         blue_pos = np.nonzero(state[1::4])[0]
    #         red_coin_pos = np.nonzero(state[2::4])[0]
    #         blue_coin_pos = np.nonzero(state[3::4])[0]
    #         if len(red_coin_pos) == 1:
    #             assert len(blue_coin_pos) == 0
    #             self.red_coin = np.array([[1]])
    #             self.coin_pos = np.array([[red_coin_pos[0] % self.grid_size, red_coin_pos[0] // self.grid_size]])
    #         else:
    #             assert len(blue_coin_pos) == 1
    #             self.red_coin = np.array([[0]])
    #             self.coin_pos = np.array([[blue_coin_pos[0] % self.grid_size, blue_coin_pos[0] // self.grid_size]])
    #         self.red_pos = np.array([[red_pos[0] % self.grid_size, red_pos[0] // self.grid_size]])
    #         self.blue_pos = np.array([[blue_pos[0] % self.grid_size, blue_pos[0] // self.grid_size]])
    #
    #     else:
    #         raise NotImplementedError()

    def _save_env(self):
        env_state = {
            "red_pos": self.red_pos, "blue_pos": self.blue_pos,
            "coin_pos": self.coin_pos, "red_coin": self.red_coin,
            "grid_size": self.grid_size, "asymmetric": self.asymmetric,
            "batch_size": self.batch_size,
            "add_position_in_epi": self.add_position_in_epi,
            "step_count": self.step_count,
            "max_steps": self.max_steps,
            "red_pick": self.red_pick,
            "red_pick_own": self.red_pick_own,
            "blue_pick": self.blue_pick,
            "blue_pick_own": self.blue_pick_own,
        }
        return copy.deepcopy(env_state)

    def _load_env(self, env_state):
        for k, v in env_state.items():
            self.__setattr__(k, v)


class AsymCoinGame(CoinGame):
    NAME = "AsymCoinGame"

    def __init__(self, config={}):
        if "asymmetric" in config:
            assert config["asymmetric"]
        else:
            config["asymmetric"] = True
        super().__init__(config)
