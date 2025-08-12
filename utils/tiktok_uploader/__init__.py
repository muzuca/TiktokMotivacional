"""
TikTok Uploader Initialization
"""

from os.path import abspath, join, dirname
import toml

# Load Config
src_dir = abspath(dirname(__file__))
config = toml.load(join(src_dir, "config.toml"))