from abc import ABC, abstractmethod


class InfoAccumulationInterface(ABC):

    @abstractmethod
    def _init_info(self):
        raise NotImplementedError()

    @abstractmethod
    def _reset_info(self):
        raise NotImplementedError()

    @abstractmethod
    def _get_episode_info(self):
        raise NotImplementedError()

    @abstractmethod
    def _accumulate_info(self, ac0, ac1):
        raise NotImplementedError()
