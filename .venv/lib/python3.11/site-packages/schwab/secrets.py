import abc

class SchwabSecrets(abc.ABC):
    @abc.abstractmethod
    def api_key():
        pass

    @abc.abstractmethod
    def app_secret():
        pass
