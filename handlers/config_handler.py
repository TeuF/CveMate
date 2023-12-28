import configparser

def singleton(cls):
    instances = {}

    def get_instance(*args, **kwargs):
        if cls not in instances:
            instances[cls] = cls(*args, **kwargs)
        return instances[cls]

    return get_instance

@singleton
class ConfigHandler:
    def __init__(self, config_file='configuration.ini'):
        self.config_file = config_file
        self.config = configparser.ConfigParser()
        self.config.read(config_file)

    def get_cvemate_config(self):
        """ Retrieve the cvemate configuration. """
        return {k: v for k, v in self.config['cvemate'].items()}
    
    def get_mongodb_config(self):
        """ Retrieve the mongodb configuration. """
        return {k: v for k, v in self.config['mongodb'].items()}

    def get_nvd_config(self):
        """ Retrieve the nvd configuration. """
        return {k: v for k, v in self.config['nvd'].items()}

    def get_config_section(self, section):
        """ Retrieve a specific section from the configuration. """
        return {k: v for k, v in self.config[section].items()}