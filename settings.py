import os

PROJECT_DIR = os.path.abspath(os.path.dirname(__file__))
APP_DIR = os.path.join(PROJECT_DIR, 'apps')
MONGREL2_SETTINGS_FILE = os.path.join(PROJECT_DIR, 'conf/mongrel2.conf')
MONGREL2_DB = os.path.join(PROJECT_DIR, 'conf/config.sqlite')
