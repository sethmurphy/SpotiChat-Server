#!/usr/bin/env python
from brubeck.auth import authenticated
from brubeck.request_handling import Brubeck, JSONMessageHandler, cookie_encode, cookie_decode
from brubeck.templating import load_jinja2_env, Jinja2Rendering
from dictshield import fields
from dictshield.document import Document
from dictshield.fields import EmbeddedDocument, ShieldException
from gevent import Greenlet
from gevent.event import Event
from urllib import unquote, quote
import sys
import logging
import httplib
import os
import time
import random
import datetime
import oauth
import spotichat

try:
    import redis
except Exception:
    raise Exception("Y U no install Redis?")

from config import ENVIRONMENT

logging.basicConfig(level=logging.DEBUG)
httplib.HTTPConnection.debuglevel = 1


## this allows us to import the demo as a module for unit tests without running the server
##
## runtime configuration
##
project_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

config = {
    'mongrel2_pair': ('ipc://run/mongrel2_send', 'ipc://run/mongrel2_recv'),
    'handler_tuples': [
        (r'^/api/feed/(?P<channel_id>.+)$', spotichat.FeedHandler),
        (r'^/api/login/(?P<channel_id>.+)/(?P<username>.+)$', spotichat.ChannelLoginHandler),
        (r'^/api/login/(?P<username>.+)$', spotichat.LoginHandler),
        (r'^/oauth/facebook/callback', oauth.FacebookOAuthCallbackHandler),
        (r'^/oauth/facebook/login/(?P<username>.+)$', oauth.FacebookOAuthRedirectorHandler),
        (r'^/oauth/tumblr/callback', oauth.TumblrOAuthCallbackHandler),
        (r'^/oauth/tumblr/login/(?P<username>.+)$', oauth.TumblrOAuthRedirectorHandler),
        (r'^/oauth/twitter/callback', oauth.TwitterOAuthCallbackHandler),
        (r'^/oauth/twitter/login/(?P<username>.+)$', oauth.TwitterOAuthRedirectorHandler),
    ],
    'cookie_secret': '_1sRe%%66a^O9s$4c6lq#0F%$9AlH)-6OO1!'
}


##
## get us started!
##


try:
    ## attach to our redis server
    ## we do all the setup here, so if we fail at anything our flag is set properly right away and we only use in memory buffer from the start            
    spotichat.redis_server = redis.Redis(host='localhost', port=6379, db=0)
    redis_server = spotichat.redis_server
    
    ## start one client to handle all messages
    ## This could be moved into the ChatChannel if performance becomes an issue
    redis_client = redis_server.pubsub()
    redis_client.subscribe(ENVIRONMENT['REDIS_PREFIX'] + 'chat_messages')
    redis_new_chat_messages = redis_client.listen()

    ## spawn out the process to listen for new messages in redis
    g1 = Greenlet(spotichat.redis_chat_messages_listener, redis_server, redis_new_chat_messages)
    g1.start()
    logging.info("started redis listener")

    logging.info("succesfully connected to redis")

except Exception:
    spotichat.set_redis_server(None)
    raise

## spawn out online user checker to timeout users after inactivity
## if there is more than one instance, this will still run in every instance
## that is probably not a very good thing we should deal with eventually
## maybe publish a "last_online_user_check" message allowing each instance to only really process if needed?
g = Greenlet(spotichat.check_users_online, redis_server)
g.start()

app = Brubeck(**config)
## start our server to handle requests
if __name__ == "__main__":
    app.run()
