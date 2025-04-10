#!/usr/bin/env python
import configparser
import json
import os
from typing import Dict, Any, Optional

class ConfigManager:
    """Manages configuration for Hermes Research Manager."""
    
    def __init__(self, config_dir: str, config_file: str, default_config: Dict[str, Dict[str, str]]):
        """Initialize the configuration manager.
        
        Args:
            config_dir: Directory to store configuration
            config_file: Path to configuration file
            default_config: Default configuration structure
        """
        self.config_dir = config_dir
        self.config_file = config_file
        self.default_config = default_config
        self.config = self._load_config()
    
    def _load_config(self) -> configparser.ConfigParser:
        """Load configuration from file or create default if it doesn't exist."""
        config = configparser.ConfigParser()
        
        # Set default config
        for section, options in self.default_config.items():
            if not config.has_section(section):
                config.add_section(section)
            for option, value in options.items():
                if isinstance(value, (dict, list)):
                    # For nested dictionaries and lists, store as JSON
                    config.set(section, option, json.dumps(value))
                else:
                    # Convert to string for ConfigParser
                    config.set(section, option, str(value))
        
        # Create config directory if it doesn't exist
        if not os.path.exists(self.config_dir):
            os.makedirs(self.config_dir)
        
        # Load existing config if it exists
        if os.path.exists(self.config_file):
            config.read(self.config_file)
        else:
            # Create default config file
            with open(self.config_file, 'w') as f:
                config.write(f)
            print(f"Created default configuration file at {self.config_file}")
        
        return config
    
    def save(self) -> None:
        """Save configuration to file."""
        with open(self.config_file, 'w') as f:
            self.config.write(f)
        print(f"Configuration saved to {self.config_file}")
    
    def get(self, section: str, option: str, fallback: Any = None) -> Any:
        """Get a configuration value."""
        return self.config.get(section, option, fallback=fallback)
    
    def set(self, section: str, option: str, value: str) -> None:
        """Set a configuration value."""
        if not self.config.has_section(section):
            self.config.add_section(section)
        self.config.set(section, option, value)
    
    def get_json(self, section: str, option: str, fallback: Dict = None) -> Dict:
        """Get a JSON configuration value."""
        if fallback is None:
            fallback = {}
        try:
            value = self.config.get(section, option, fallback=json.dumps(fallback))
            return json.loads(value)
        except Exception:
            return fallback
    
    def set_json(self, section: str, option: str, value: Dict) -> None:
        """Set a JSON configuration value."""
        self.set(section, option, json.dumps(value))
