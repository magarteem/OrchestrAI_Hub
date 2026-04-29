from abc import ABC, abstractmethod


class BaseMouseControls(ABC):
    @abstractmethod
    def move_relative(self, dx: int, dy: int) -> None:
        pass
