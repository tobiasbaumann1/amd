import gym
from ray.rllib.policy.policy import Policy
from ray.rllib.utils.typing import TrainerConfigDict

from marltoolbox.utils.restore import LOAD_FROM_CONFIG_KEY


def get_tune_policy_class(PolicyClass):
    """
    Allow to convert a Tune trainer into a frozen RLLib policy (no training possible).

    :param PolicyClass: The base RLLib policy class to use. Can be needed if you need some statistics or postprocessing.
    :return: an RLLib policy class that compute actions by calling the Tune trainer.
    """
    class FrozenPolicyFromTuneTrainer(PolicyClass):

        def __init__(self, observation_space: gym.spaces.Space,
                     action_space: gym.spaces.Space, config: TrainerConfigDict):
            print("__init__ FrozenPolicyFromTuneTrainer")

            self.tune_config = config["tune_config"]
            TuneTrainerClass = self.tune_config["TuneTrainerClass"]
            self.tune_trainer = TuneTrainerClass(config=self.tune_config)
            self.load_checkpoint(config.pop(LOAD_FROM_CONFIG_KEY, (None, None)))

            super().__init__(observation_space, action_space, config)

        def compute_actions(self,
                            obs_batch,
                            state_batches=None,
                            prev_action_batch=None,
                            prev_reward_batch=None,
                            info_batch=None,
                            episodes=None,
                            **kwargs):
            actions, state_out, extra_fetches = self.tune_trainer.compute_actions(self.policy_id, obs_batch)
            return actions, state_out, extra_fetches

        def learn_on_batch(self, samples):
            raise NotImplementedError("FrozenPolicyFromTuneTrainer policy can't be trained")

        def get_weights(self):
            return {"checkpoint_path": self.checkpoint_path,
                    "policy_id": self.policy_id}

        def set_weights(self, weights):
            checkpoint_path = weights["checkpoint_path"]
            policy_id = weights["policy_id"]
            self.load_checkpoint((checkpoint_path, policy_id))

        def load_checkpoint(self, checkpoint_tuple):
            self.checkpoint_path, self.policy_id = checkpoint_tuple
            if self.checkpoint_path is not None:
                self.tune_trainer.load_checkpoint(self.checkpoint_path)

    return FrozenPolicyFromTuneTrainer


import torch
from ray.rllib.agents.a3c.a3c_torch_policy import A3CTorchPolicy, ValueNetworkMixin
from ray.rllib.policy.torch_policy import LearningRateSchedule
from ray.rllib.agents.dqn.dqn_torch_policy import setup_early_mixins


def sgd_optimizer(policy: Policy,
                  config: TrainerConfigDict) -> "torch.optim.Optimizer":
    return torch.optim.SGD(policy.model.parameters(), lr=policy.cur_lr)

A2CTorchPolicy = A3CTorchPolicy.with_updates(
    optimizer_fn=sgd_optimizer,
    before_init=setup_early_mixins,
    mixins=[ValueNetworkMixin, LearningRateSchedule])
