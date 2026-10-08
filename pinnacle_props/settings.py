"""Declarative sport, market and book settings."""
from pathlib import Path
import yaml

ROOT=Path(__file__).resolve().parents[1]


def load_config(path=ROOT/'config/scanner.yaml'):
    with Path(path).open() as f:
        config=yaml.safe_load(f)
    if not isinstance(config,dict) or not config.get('sports') or not config.get('books'):
        raise ValueError('Scanner configuration missing sports or books')
    return config
