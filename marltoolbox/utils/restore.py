import logging
import pickle

logger = logging.getLogger(__name__)

LOAD_FROM_CONFIG_KEY = "checkpoint_to_load_from"


def after_init_load_policy_checkpoint(policy, observation_space=None, action_space=None, trainer_config=None):
    """
    This function is to be given to a policy template (to the after_init argument).
    It will load a specific policy state from a given checkpoint
    (instead of all policies like the restore option provided by RLLib.

    The policy config must contain the tuple (checkpoint_path, policy_id) to load from, stored
    under the LOAD_FROM_CONFIG_KEY key.

    Finally, the checkpoint_path can be callable, in this case it must return a path (str)
    and accept the policy config as the only argument.
    This last feature allows to dynamically select checkpoints
    for example in multistage training or experiments
    Example: determining the checkpoint to load conditional on the current seed
    (when doing a grid_search over random seeds and with a multistage training)
    """
    checkpoint_path, policy_id = policy.config.pop(LOAD_FROM_CONFIG_KEY, (False, False))

    if callable(checkpoint_path):
        checkpoint_path = checkpoint_path(policy.config)

    if checkpoint_path:
        load_one_policy_checkpoint(policy_id, policy, checkpoint_path)
    else:
        print(f"RLLib loading: no checkpoint found for policy_id: {policy_id} "
              f"by looking for config key: {LOAD_FROM_CONFIG_KEY}")


def load_one_policy_checkpoint(policy_id, policy, checkpoint_path, using_Tune_class=False):
    """
    :param policy_id: the policy_id of the policy inside the checkpoint that is going to be loaded into the policy
        provided as 2nd argument
    :param policy: the policy to load the checkpoint into
    :param checkpoint_path: the checkpoint to load from
    :param using_Tune_class: to be set to True in case you are loading a policy from a Tune checkpoint
        (not a RLLib checkpoint) and that the policy you are loading into was created by converting your Tune trainer
        into frozen a RLLib policy
    :return: None
    """
    if using_Tune_class:
        # The provided policy must implement load_checkpoint.
        # This is only intended for the policy class: FrozenPolicyFromTuneTrainer
        policy.load_checkpoint(checkpoint_tuple=(checkpoint_path, policy_id))
    else:
        logger.info(f"checkpoint_path {checkpoint_path}")
        checkpoint = pickle.load(open(checkpoint_path, "rb"))
        assert "worker" in checkpoint.keys()
        assert "optimizer" not in checkpoint.keys()
        objs = pickle.loads(checkpoint["worker"])
        # TODO Should let the user decide to load that too
        # self.sync_filters(objs["filters"])
        print("WARNING: not loading objs['filters']")
        found_policy_id = False
        for p_id, state in objs["state"].items():
            if p_id == policy_id:
                print(f"going to load policy {policy_id} from checkpoint {checkpoint_path}")
                policy.set_state(state)
                found_policy_id = True
                break
        if not found_policy_id:
            print(f'policy_id {policy_id} not in checkpoint["worker"]["state"].keys() {objs["state"].keys()}')
