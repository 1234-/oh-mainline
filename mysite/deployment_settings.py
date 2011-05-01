# This settings file is loaded by ./bin/production, which is
# what we use on the main OpenHatch deployment.
#
# The live site needs some slightly different settings.
#
# So we start by loading the settings module in the same directory...
from settings import *
# ...and then we override some values.

# But use the linode as our MySQL server
DATABASE_HOST='linode.openhatch.org'

OHLOH_API_KEY='SXvLaGPJFaKXQC0VOocAg'
DEBUG=False
ADMINS=[
    ('Private Server Monitoring List', 'monitoring-private@lists.openhatch.org',)
]

INVITE_MODE=False # Suckas, invite codes are disabled everywarez
INVITATIONS_PER_USER=20

TEMPLATE_DEBUG=False

EMAIL_SUBJECT_PREFIX='[Kaboom@OH] '

SEND_BROKEN_LINK_EMAILS=True
MANAGERS=ADMINS
SERVER_EMAIL='mr_website@linode.openhatch.org'

CACHE_BACKEND = "memcached://127.0.0.1:11211/?timeout=1"

POSTFIX_FORWARDER_TABLE_PATH = '/etc/postfix/virtual_alias_maps'

CELERY_ALWAYS_EAGER = False # srsly

## always use linode-one for 
## AMQP, Rabbit Queue, Celery
CARROT_BACKEND = 'amqp'

BROKER_HOST = 'linode.openhatch.org'
BROKER_PORT = 5672
BROKER_USER = 'rabbiter'
BROKER_PASSWORD = 'johT4qui'
BROKER_VHOST = 'localhost'

### always use memcached on linode-one, also
CACHE_BACKEND = "memcached://linode.openhatch.org:11211/?timeout=1"

try:
    from deployment_settings_secret_keys import GOOGLE_ANALYTICS_CODE
except ImportError:
    pass

PATH_TO_MANAGEMENT_SCRIPT = '/home/deploy/milestone-a/bin/production'
GIT_REPO_URL_PREFIX = 'http://openhatch.org/git-missions/'
