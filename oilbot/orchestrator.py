from oilbot.config import Config


class Orchestrator:
    def __init__(self, config: Config) -> None:
        self.config = config

    async def run(self) -> None:
        raise NotImplementedError
