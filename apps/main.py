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
import urllib2
import functools
import logging
import os
import time
import md5
import base64
import hmac
import hashlib
import httplib
import random
import requests
import json

from config import ENVIRONMENT
from config import TWITTER
from config import FACEBOOK

logging.basicConfig(level=logging.DEBUG)

## add redis support if available
## redis also handles persistance and synching all brubeck chatify instances
## without redis only a single instance can be run
using_redis = False
redis_prefix = 'sp:'

try:
    import redis
    import json
    using_redis = True
except Exception:
    logging.info("redis module not found (single instance mode: using in memory buffer)")
    pass


BUFFER_SIZE = 50

## holds all our ChatChannel objects
chat_channels = {}

## Our long polling interval
POLLING_INTERVAL = 15

## Our users check interval
USER_TIMEOUT_INTERVAL = 30

## How old a user must be in seconds to kick them out of the room
USER_TIMEOUT = 60

def find_user_by_username(username):
    """returns a user by nickname, trying redis first"""
    if username == None:
        return None;
        
    if using_redis:
        user = redis_find_user_by_username(username)
    else:
        user = list_find_user_by_username(username)
    return user

def list_find_user_by_username(username):
    """returns the first list item matching a usename"""
    logging.info("finding %s in user list " % username)
    users = filter(lambda x: x.username == username,
                   users_online)
    if len(users)==0:
        return None
    else:
        return users[0]

def redis_find_user_by_username(username):
    """returns the user by username"""
    logging.info("finding user %s in redis " % username)
    key = redis_prefix + "users:%s" % (username)
    data = redis_server.get(key)

    if data != None:
        logging.info("found user by username (%s):  %s" % (key, data))
        return User(**json.loads(data))
    else:
        logging.info("unable to find user by username (%s): '%s'" % (key, username))
        return None


##
## Methods to add a user to the chat server
## We never remove them, that would be impolite
##

def add_user(user):
    """add a user to our users."""
    """ the xxx_add_user_message wrapper, uses redis by default if possible"""
    try:
        if using_redis:
            redis_add_user(user)
        else:
            list_add_user(user)
    except Exception, e:
        logging.info("error adding user %s: %s" % (user, e))

def list_add_user(user):
    """add a user to our users"""
    users_online.append(user)

def redis_add_user(user):
    """adds a user to the redis server and publishes it"""

    data = user.to_json()
    key = "%s" % (user.username)

    logging.info("adding new user timestamp: %s" % key)

    # add our user object to a simple set, keyed by username
    key = redis_prefix + "users:%s" % key

    affected = redis_server.set(key, data)
    logging.info("added new user (%s): %s" % (affected, key))

def get_chat_channel(channel_id):
    """finds or creates a new ChatChannel"""
    if channel_id not in chat_channels:
        chat_channel = ChatChannel(channel_id, redis_server)
        chat_channels[channel_id] = chat_channel
        logging.info("Created chat channel with channel name %s" % (chat_channel.name))
    else:
        chat_channel = chat_channels[channel_id]
        logging.info("Found chat channel with channel name %s" % (chat_channel.name))
    return chat_channel

##
## our redis channel listeners
##

def redis_chat_messages_listener(redis_server):
    """listen to redis for when new messages are published"""
    logging.info("Spun up a redis chat message listener.")
    while True:
        raw = redis_new_chat_messages.next()
        msg = (ChatMessage(**json.loads(raw['data'])))
        ## just hook into our existing way for now
        ## a bit redundant but allows server to be run without redis
        logging.info("new chat message subscribed to: %s" % raw['data'])
        ## add o our local buffer to push to clients
        chat_channel = get_chat_channel(msg.channel_name)
        chat_channel.list_add_chat_message(msg)


#
# Our channel definition class
#

class ChatChannel(object):
    """encapsulates a chat channel"""

    def __init__(self, channel_id, redis_server = None, buffer_size = BUFFER_SIZE):
        self.channel_id = channel_id
        self.name = channel_id
        ## this is how we alert our member of new stuff
        self.new_message_event = Event()

        ## hold our messages in memory here, limit to last 50
        ## persistance is handled by redis, or not at all
        self.chat_messages = []
        self.users_online = [] # only used for in memory
        self.buffer_size = BUFFER_SIZE;
        self._load_buffer()
        self.redis_server = redis_server
        self.using_redis = (redis_server != None)

    def _load_buffer(self):
        try:
            ## fill the in memory buffer with redis data here
            key = redis_prefix + "chat_messages:%s" % self.channel_id
            msgs = redis_server.lrange(key, -1 * self.buffer_size, -1)
            i = 0
            for msg in msgs:
                try:
                    message = ChatMessage(**json.loads(msg))
                    self.chat_messages.append(message)
                    i += 1
                except Exception, e:
                    logging.info("error loading message %s: %s" % (msg, e))

            logging.info("loaded chat_messages for %s memory buffer (%d items)" % (key, i))

        except Exception, e:
            logging.info("failed to load messages for channel %s from redis: %s" % (self.channel_id, e))

    def wait(self, seconds):
        """wait for a new message"""
        logging.info("sleeping")
        self.new_message_event.wait(seconds)
        logging.info("waking")

    ##
    ## Methods to add a chat message
    ##

    def add_chat_message(self, message):
        """ the xxx_add_chat_message wrapper, uses redis by default if possible"""
        try:
            if self.using_redis:
                self.redis_add_chat_message(message)
            else:
                self.list_add_chat_message(message)
        except Exception, e:
            logging.info("error adding message %s: %s" % (message, e))

    def list_add_chat_message(self, chat_message):
        """Adds a message to our message history. A server timestamp is used to
        avoid sending duplicates."""
        self.chat_messages.append(chat_message)

        #logging.info("adding message: %s" % chat_message.message)

        if len(self.chat_messages) > BUFFER_SIZE:
            self.chat_messages.pop(0)

        # alert our polling clients
        self.new_message_event.set()
        self.new_message_event.clear()

    def redis_add_chat_message(self, chat_message):
        """adds a message to the redis server and publishes it"""
        data = chat_message.to_json()
        key = redis_prefix + "chat_messages:%s" % self.channel_id

        logging.info(data)

        self.redis_server.rpush(key, data)
        self.redis_server.publish(redis_prefix + 'chat_messages', data)

    def get_messages(self, since_timestamp=0):
        """get new messages since a certain timestamp"""
        return filter(lambda x: x.timestamp > since_timestamp,
                      self.chat_messages)

    ##
    ## Methods to add a user to the channel
    ##

    def add_user(self, user):
        """add a user to our online users. Timestamp used to determine freshness"""
        """ the xxx_add_user_message wrapper, uses redis by default if possible"""
        try:
            if self.using_redis:
                self.redis_add_user(user)
            else:
                self.list_add_user(user)
        except Exception, e:
            logging.info("error adding user %s: %s" % (user, e))

    def list_add_user(self, user):
        """add a user to our online users. Timestamp used to determine freshness"""
        self.users_online.append(user)

    def redis_add_user(self, user):
        logging.info("channel redis add_user")
        """adds a user to the redis server and publishes it"""

        data = user.to_json()
        key = "%s:%s" % (self.channel_id, user.username)

        logging.info("adding new user timestamp: %s" % key)
        # add our username to a set orderes by timestamp to be able to quickly purge
        affected = self.redis_server.zadd(redis_prefix + "users_timestamp",key, user.timestamp)
        logging.info("added new user timestamp(%s): %s:%s" % (affected, key, user.timestamp))
#        # add our user object to a simple set, keyed by username
#        key = redis_prefix + "users:%s" % key
#
#        affected = self.redis_server.set(key, data)
#        logging.info("added new user (%s): %s" % (affected, key))


    ##
    ## Methods to remove a user
    ##
    def remove_user(self, user):
        """remove a user from our users_online"""
        """ the xxx_remove_user wrapper, uses redis by default if possible"""
        if self.using_redis:
            self.redis_remove_user(user)
        else:
            self.list_remove_user(user)

    def list_remove_user(self, user):
        """remove a user from a list"""
        for i in range(len(self.users_online)):
            if self.users_online[i].username == user.username:
                del self.users_online[i]
                break

    def redis_remove_user(self, user):
        """removes a user from the redis server and publishes it"""

        data = user.to_json()
        key = "%s:%s" % (self.channel_id, user.username)

        logging.info(data)
        # remove our users timestamp
        affected = self.redis_server.zrem(redis_prefix + 'users_timestamp',key)
        logging.info("removed user timestamp(%d): %s" % (affected, key))
#        # remove our user 
#        key = redis_prefix + "users:%s" % (key)
#        affected = self.redis_server.expire(key, 0)
#        logging.info("removed user(%d): %s" % (affected, key))

    ##
    ## Update our users timestamp methods
    ##

    def update_user_timestamp(self, user):
        """ the xxx_update_user_timestamp wrapper, uses redis by default if possible"""
        user.timestamp = int(time.time())
        if using_redis:
            return self.redis_update_user_timestamp(user)
        else:
            return self.list_update_user_timestamp(user)


    def list_update_user_timestamp(self, user):
        """updates the timestamp on the user to avoid expiration"""
        usr = find_user_by_username(user.username)
        if usr != None:
            usr.timestamp = user.timestamp

        return usr

    def redis_update_user_timestamp(self, user):
        """timestamps our active user and publishes the changes"""
        data = user.to_json()
        key = "%s:%s" % (self.channel_id, user.username)

        logging.info("updating users timestamp: %s" % key)
        # update our timestamp ordered set
        
        affected = self.redis_server.zadd(redis_prefix + "users_timestamp", key, user.timestamp)

        return user

    def find_user_by_username(self, username):
        """returns a user by nickname, trying redis first"""
        if self.using_redis:
            user = self.redis_find_user_by_username(username)
        else:
            user = self.list_find_user_by_username(username)
        return user

    def list_find_user_by_username(self, username):
        """returns the first list item matching a usename"""
        logging.info("finding %s in user list " % username)
        users = filter(lambda x: x.username == username,
                       self.users_online)
        if len(users)==0:
            return None
        else:
            return users[0]

    def redis_find_user_by_username(self, username):
        user = None
        """returns the user by username"""
        logging.info("channel finding %s in redis " % username)
        key = "%s:%s" % (self.channel_id, username)
        # see if we have a timestamp in the room
        rank = self.redis_server.zrank(redis_prefix + "users_timestamp", key)
        logging.info("channel %s users_timestamp rank (%s): %s " % (redis_prefix, key, rank))
        if rank != None:
            # get our user from the chat server
            logging.info("found users_timestamp, fetching user")
            user = find_user_by_username(username)

        if user != None:
            logging.info("found user by username (%s):  %s" % (key, username))
            return user
        else:
            logging.info("channel unable to find user by username (%s): '%s'" % (key, username))
            return None


##
## Check online user methods
## users are stored associated with their channels
##

def check_users_online():
    """check for expired users and send a message they left the room"""
    before_timestamp = int((time.time()) - (USER_TIMEOUT))

    logging.info("checking users online, purging before %s" % before_timestamp)

    if using_redis:
        redis_check_users_online(before_timestamp)
    else:
        list_check_users_online(before_timestamp)

    ## setup our next check
    g = Greenlet(check_users_online)
    g.start_later(USER_TIMEOUT_INTERVAL)

def list_check_users_online(before_timestamp):
    """check for expired users and send a message they left the room"""
    for chat_channel in chat_channels:
        expired_users = filter(lambda x: x.timestamp <= before_timestamp,
                       chat_channel.users_online)
        for user in expired_users:
            msg = ChatMessage(nickname='system', username='system', message="%s can not been found in the room %s" % (user.nickname, chat_chanel.channel_name), channel_name = chat_channel.name);

            chat_channel.add_chat_message(msg)
            chat_channel.remove_user(user)

def redis_check_users_online(before_timestamp):
    """check for expired users and send a message they left the room"""
    logging.info("checking for users before: %s" % before_timestamp)    
    expired_users_count = redis_server.zcount(redis_prefix + "users_timestamp",0,before_timestamp)
    logging.info("found %d users to expire" % expired_users_count)
    if expired_users_count > 0:
        expired_users = redis_server.zrange(redis_prefix + "users_timestamp",0, expired_users_count)
        if expired_users != None:
            for key in expired_users:
                channel_name = key.split(':')[0]
                username = key.split(':')[1]
                key = redis_prefix + "users:%s" % username
                data = redis_server.get(key)
                if data != None:
                    user = User(**json.loads(data))

                    msg = ChatMessage(nickname='system', username='system', message="%s can not been found in the room" % user.nickname, channel_name = channel_name);
                    
                    chat_channel = get_chat_channel(channel_name)
                    chat_channel.add_chat_message(msg)
                    chat_channel.remove_user(user)
                else:
                    logging.info("unable to find expired user: %s" % (key))


import re, htmlentitydefs

#
# Removes HTML or XML character references and entities from a text string.
#
# @param text The HTML (or XML) source text.
# @return The plain text, as a Unicode string, if necessary.
#
# The things we do for unicode snowman support ...  
#
def unescape(text):
    """unescapes HTML entities"""
    def fixup(m):
        text = m.group(0)
        if text[:2] == "&#":
            # character reference
            try:
                if text[:3] == "&#x":
                    return unichr(int(text[3:-1], 16))
                else:
                    return unichr(int(text[2:-1]))
            except ValueError:
                pass
        else:
            # named entity
            try:
                text = unichr(htmlentitydefs.name2codepoint[text[1:-1]])
            except KeyError:
                pass
        return text # leave as is
    return re.sub("&#?\w+;", fixup, text)


##
## Our dictshield class defintions
##

class User(Document):
    """a chat user"""
    timestamp = fields.IntField(required=True)
    username = fields.StringField(required=True, max_length=40)
    nickname = fields.StringField(required=True, max_length=40)
    oauth_provider = fields.StringField(max_length=40)
    oauth_id = fields.StringField(required=True, max_length=40)
    oauth_data = fields.StringField(required=True, max_length=2048)

    def __init__(self, *args, **kwargs):
        super(User, self).__init__(*args, **kwargs)
        
        # seconds is enough here, we need an int
        self.timestamp = int(time.time())

class ChatMessage(EmbeddedDocument):
    """A single chat message"""
    timestamp = fields.IntField(required=True)
    username = fields.StringField(required=True, max_length=40)
    nickname = fields.StringField(required=True, max_length=40)
    message = fields.StringField(required=True)
    msgtype = fields.StringField(default='user',
                          choices=['user', 'error', 'system'])
    channel_name = fields.StringField(required=True, max_length=30)

    def __init__(self, *args, **kwargs):
        super(ChatMessage, self).__init__(*args, **kwargs)
        self.timestamp = int(time.time() * 1000)

##
## Our handler class definitions
##

class ChatifyJSONMessageHandler(JSONMessageHandler):
    """our JSON message handlers base class"""
    def prepare(self):
        """get our user from the request and set to self.current_user"""
        self.current_user = None
        self.username = None
        self.channel = None
        self.channel_id = None

        # self._url_args only has a list 
        # we need a dictonary with the named parameters
        # so, we reparse the url

        try:
            print self._url_args
      
            self.username = self.get_cookie('username', None, self.application.cookie_secret)
            if self.username != None:
                logging.info("username from cookie: %s " % (self.username))

            if hasattr(self, '_url_args') and 'channel_id' in self._url_args:
                self.channel_id =  unquote(self._url_args['channel_id']).decode('utf-8')
                logging.info("channel_id from url_args: %s " % (self.channel_id))

            if self.channel_id == None:
                self.channel_id = self.get_argument('channel_id', 'public')
                logging.info("channel_id from arguments: %s " % (self.channel_id))

            self.channel = get_chat_channel(self.channel_id)

            if self.username == None:
                self.username = self.get_argument('username', None)
                if self.username != None:
                    logging.info("username from arguments: %s " % (self.username))
                    self.username = cookie_decode(self.username, self.application.cookie_secret)
            if self.username == None:
                if hasattr(self, '_url_args') and 'username' in self._url_args:
                    self.username =  unquote(self._url_args['username']).decode('utf-8')
                    logging.info("username from url_args: %s " % (self.username))

            if self.username != None and self.channel != None:
                self.username = unescape(self.username)
                # user = self.channel.find_user_by_username(self.username)
                user = find_user_by_username(self.username)
                if user != None:
                    self.current_user = self.channel.update_user_timestamp(user)
                    logging.info("set current user %s" % self.current_user.username)


        except Exception:
            raise

        self.headers['Access-Control-Allow-Origin'] = 'sp://spotichat'
        self.headers['Access-Control-Allow-Credentials'] = 'true'
        self.headers['Access-Control-Allow-Headers'] = 'Origin, Content-Type, User-Agent, Accept, Cache-Control, Pragma, Set-Cookie'
        self.headers['Access-Control-Allow-Methods'] = 'POST, GET, DELETE, OPTIONS'


    def get_current_user(self):
        """return  self.current_user set in self.prepare()"""
        return self.current_user

class ChatifyOAuthHandler(JSONMessageHandler):
    """our JSON message handlers base class"""
    def prepare(self):
        """get our user from the request"""
        self.username = None

        try:
            if hasattr(self, '_url_args') and 'username' in self._url_args:
                self.username =  unquote(self._url_args['username']).decode('utf-8')
                if not self.username is None:
                    logging.info("username from url_args: %s " % (self.username))

            if self.username is None:
                self.username = self.get_cookie('username', None, self.application.cookie_secret)
                if not self.username is None:
                    logging.info("username from cookie: %s " % (self.username))
                else:
                    self.username = self.get_argument('username', None)
                    if not self.username is None:
                        logging.info("username from query string: %s " % (self.username))
                    else:
                        logging.info("No username!")
                        
        except Exception:
            raise

        if self.username is None or len(self.username) == 0:
            self.set_status(403, "username required")


class TwitterOAuthHandler(ChatifyOAuthHandler):
    """Handles twitter ouath authentication"""
    def _signature_base_string(self, http_method, base_uri, query_params, delimiter = "%26"):
        """Creates the base string ro an authorized twitter request"""
        query_string = ''
        for key, value in query_params:
            if query_string != '':
                query_string = query_string + delimiter
            if value != '' and key != '':
                query_string = query_string + quote( key , '') + "%3D" + quote( value, '' )

        return http_method + "&" + quote(  base_uri, '' ) + "&" + query_string

    def _sign(self, secret_key, base_string ):
        """Creates a HMAC-SHA1 signature"""
        digest = hmac.new(secret_key, base_string, hashlib.sha1).digest()
        return base64.encodestring(digest).rstrip()

    def _authorization_header(self, query_params):
        authorization_header = 'OAuth'
        
        for key, value in query_params:
            #print key
            #print value
            if value != '':
                authorization_header = authorization_header + ' ' + key  + '="' + quote( value, '' ) + '",'
        authorization_header = authorization_header.rstrip(',')
        
        return authorization_header
        
    def _generate_nonce(self):
        random_number = ''.join(str(random.randint(0, 9)) for i in range(40))
        m = md5.new(str(time.time()) + str(random_number))
        return m.hexdigest()

  
    def get(self):
        url = TWITTER['REQUEST_TOKEN_URL']
        time_stamp = str(int(time.mktime(time.gmtime())))
        #time_stamp = str( int( time.time() ) )
        nonce = self._generate_nonce()
        oauth_token = '';
        oauth_secret = '';
        oauth_callback = TWITTER['CALLBACK_URL'];
        
        #oauth_token = '266225623-HBjdxl2hgznbDbiAznVM4EySWM1WTUDGMxotiPcQ';
        #oauth_secret = 'oW7oJn7SMe4PSy55lWy7v2taySVAcswNwfkfH3nLOQ';
        #oauth_callback = ''
        #nonce = 'a95d682aa2acc023642314b1f2cf75a4'
        #time_stamp = '1323788861'
        
        query_params = [
            ('oauth_callback', oauth_callback),
            ('oauth_consumer_key', TWITTER['CONSUMER_KEY']),
            ('oauth_nonce', nonce),
            ('oauth_signature_method', 'HMAC-SHA1'),
            ('oauth_timestamp', time_stamp),
            ('oauth_token', oauth_token),
            ('oauth_version', '1.0')
        ]
        signature_base_string = self._signature_base_string('POST', url, query_params)
        signature_key = TWITTER['CONSUMER_SECRET'] + "&" + oauth_secret;
        signature = self._sign(signature_key, signature_base_string)
        
        print signature_key + "\n\n"
        print signature_base_string + "\n\n"
        print signature + "\n\n"

        query_params = [ 
            ('oauth_nonce', nonce),
            ('oauth_callback', oauth_callback),
            ('oauth_signature_method', 'HMAC-SHA1'),
            ('oauth_token', oauth_token),
            ('oauth_timestamp', time_stamp),
            ('oauth_consumer_key', TWITTER['CONSUMER_KEY']),
            ('oauth_signature', signature),
            ('oauth_version', '1.0')
        ]


        #httplib.HTTPConnection.debuglevel = 1
        #opener = urllib2.build_opener(urllib2.HTTPHandler(debuglevel=1))
        #urllib2.install_opener(opener)

        authorization_header = self._authorization_header(query_params)
        print authorization_header + "\n\n"

        #req = urllib2.Request(url, {},)
        #req.add_header('Authorization', authorization_header);
        
        the_page = "error"

        try:
            #response = urllib2.urlopen(req)
            #response = opener.open(req)
            #the_page = response.read()
            response = requests.post(url, {}, **{'headers': { 'Authorization': authorization_header } } )
            the_page = response.content
            #print response.history
            #print response.headers
        except Exception:
            raise
        #print req.headers
        print the_page + "\n\n"


        self.set_status(200)

        self.add_to_payload('messages', the_page)
        return self.render()


class FacebookOAuthRedirectorHandler(ChatifyOAuthHandler):
    """Handles Facebook oauth authentication"""

    def get(self, *args, **kwargs):

        self.set_cookie('username', "9cd92be2ed90a04e8d1f178512b3d24331944617", self.application.cookie_secret, domain="spotichat.com")

        # start the login process
        url = "%s?client_id=%s&scope=%s&redirect_uri=%s&display=popup" % (FACEBOOK['REQUEST_URI'], FACEBOOK['APP_ID'], FACEBOOK['SCOPE'], FACEBOOK['REDIRECT_URI'] + "?username=" + self.username)
        if  ENVIRONMENT.DEBUG == True:
            # we are in development, fake it
            logging.debug( "facebook faking it" );
            user = find_user_by_username(self.username)
            # we should store this data now
            if user == None:
                user = User(username=self.username, nickname=oauth_data['username'], oauth_id=oauth_data['id'], oauth_provider='facebook', oauth_data=json.dumps(oauth_data))
            else:
                user.nickname = oauth_data['username']
                user.oauth_id = oauth_data['id']
                user.oauth_provider = 'facebook'
                user.oauth_data = json.dumps(oauth_data)
            
            # adding an existing key just replaces it
            add_user(user)

            return self.redirect("/oauth/facebook/loggedin")

        # send user to facebook login
        logging.debug( "facebook url %s" % url );
        return self.redirect(url)


class FacebookOAuthCallbackHandler(ChatifyOAuthHandler):
    """Handles Facebook oauth authentication"""

    def _parse_content(self, content):
        """Parses a key value pair or JSON string into a dict"""
        kv_dict = {}
        if content[0] == '{':
            # assume JSON
            kv_dict = json.loads(content)
    
        else:
            kv_pairs = content.split('&')
            for kv_pair in kv_pairs:
                kv = kv_pair.split('=')
                if len(kv) == 2:
                    kv_dict[kv[0]]=kv[1] 

        return kv_dict  

    def get(self):
        code = self.get_argument('code', '')
        # we came from a callback and have our code    
        logging.debug( "facebook callback" );
        url = FACEBOOK['ACCESS_TOKEN_REQUEST_URI']
        fields = { 
            'client_id': FACEBOOK['APP_ID'],
            'redirect_uri': FACEBOOK['REDIRECT_URI'] + "?username=" + self.username,
            'client_secret': FACEBOOK['APP_SECRET'],
            'code': code
        }
        
        response = requests.post(url, fields)
        access_token_info = self._parse_content(response.content)

        # if succesfull in getting token, get some about me info
        user = find_user_by_username(self.username)

        if 'access_token' in access_token_info:
            logging.debug( "access_token_info %s" % access_token_info );
            # get a little more data about the user (me query)
            url =  "%s?access_token=%s" % (FACEBOOK['ME_REQUEST_URI'], access_token_info['access_token'])
            response = requests.get(url)
            me_info = self._parse_content(response.content)
            logging.debug( "me_info %s" % me_info );
            access_token_info.update(me_info)
            oauth_data = access_token_info
            logging.debug( "oauth_info %s" % access_token_info );
            # we should store this data now
            if user == None:
                user = User(username=self.username, nickname=oauth_data['username'], oauth_id=oauth_data['id'], oauth_provider='facebook', oauth_data=json.dumps(oauth_data))
            else:
                user.nickname = oauth_data['username']
                user.oauth_id = oauth_data['id']
                user.oauth_provider = 'facebook'
                user.oauth_data = json.dumps(oauth_data)
            
            # adding an existing key just replaces it
            add_user(user)

            return self.redirect("/oauth/facebook/loggedin")
        else:
            self.set_status(403)
            self.add_to_payload('messages', "Not Authenticated")

        return self.render()




        
class FeedHandler(ChatifyJSONMessageHandler):
    """Handles poll requests from user; sends out queued messages."""
 
    def _get_messages(self):
        """checks for new messages"""
        try:
            messages = self.channel.get_messages(int(self.get_argument('since_timestamp', 0)))

        except ValueError as e:
            messages = self.channel.get_messages()

        return messages

    @authenticated
    def get(self, channel_id):
        """gets any recent messages, or waits for new ones to appear"""
        
        messages = self._get_messages()

        if len(messages)==0:
            # we don't have any messages so sleep for a bit
            self.channel.wait(POLLING_INTERVAL)

            # done sleeping or woken up
            #check again and return response regardless
            messages = self._get_messages()

        self.set_status(200)
        self.add_to_payload('messages', messages)

        return self.render()

    @authenticated
    def post(self, channel_id):
        message = unescape(self.get_argument('message'))
        logging.info("%s: %s" % (self.username, message))
        msg = ChatMessage(**{'nickname': self.current_user.nickname, 'username': self.current_user.username, 'message': message,
                             'channel_name': self.channel_id})

        try:
            msg.validate()
            self.channel.add_chat_message(msg)

            self.set_status(200);
            self.add_to_payload('message','message sent')

        except ShieldException, se:
            self.set_status(403, 'VALIDATION ERROR: %s' % (se));

        return self.render()

class ChannelLoginHandler(ChatifyJSONMessageHandler):
    """Allows users to enter the chat room.  Does no authentication."""

    @authenticated
    def post(self, channel_id, username):
        message = self.get_argument('message', '%s has entered the room.' % username)
        username = unquote(username).decode('utf-8')
        nickname = self.current_user.nickname
        if username != None and len(username) != 0:

            if username == self.username :
                logging.info("channel logging in user %s to %s" % (username, channel_id))
                user = self.channel.add_user(User(username=self.username, nickname=nickname))
                msg = ChatMessage(timestamp=int(time.time() * 1000), username='system', nickname='system',
                    message=message, msgtype='system', channel_name=self.channel_id)

                self.channel.add_chat_message(msg)

                ## respond to the client our success
                self.set_status(200)
                self.add_to_payload('message', nickname + ' has entered the chat room')

            else:
                ## let the client know we failed because they didn't ask nice
                self.set_status(403, 'identity theft is a serious crime')

        else:
            ## let the client know we failed because they didn't ask nice
            self.set_status(403, 'missing username argument')
        return self.render()
        
    @authenticated
    def delete(self, channel_id, username):
        """ remove a user from the chat session"""
        username = unquote(username).decode('utf-8')

        if self.username != None and len(self.username) != 0 and self.username == username:

            ## remove our user and alert others in the chat room
            user = self.channel.find_user_by_username(self.username)

            if user != None:
                message = self.get_argument('message', '%s has left the room.' % user.nickname)
                self.channel.remove_user(user)
                msg = ChatMessage(timestamp=int(time.time() * 1000), username='system', nickname='system',
                   message=message, msgtype='system', channel_name=self.channel_id)

                self.channel.add_chat_message(msg)

                ## respond to the client our success
                self.set_status(200)
                self.add_to_payload('message',unquote(user.nickname) + ' has left the chat room')

            else:
                ## let the client know we failed because they were not found
                self.set_status(403, 'User not found, Authentication failed')

        else:
            ## let the client know we failed because they didn't ask nice
            self.set_status(403, 'missing username argument')
        return self.render()

class LoginHandler(ChatifyJSONMessageHandler):
    """Handles authentication only. If sucsessful a SPOTICHATSESSID will be set"""

    def post(self, username):
        # right now a nickname and username are the same, however that will change
        if username != None and len(username) != 0:
            user = find_user_by_username(username)
            nickname = username
            if user == None :
                logging.info("adding user %s." % (username))
                user = add_user(User(username=username, nickname=nickname))
            else:
                nickname = user.nickname
                logging.info("logging in user %s." % (username))
                
            ## respond to the client our success
            self.set_status(200)
            self.set_cookie('username', username.encode('utf-8'), self.application.cookie_secret, domain="spotichat.com")
            self.set_cookie('nickname', nickname, domain="spotichat.com")

            self.add_to_payload('message', nickname + ' has entered the chat room')
            self.add_to_payload('username', cookie_encode(username.encode('utf-8'), self.application.cookie_secret))
            self.add_to_payload('nickname', nickname)

        else:
            ## let the client know we failed because they didn't ask nice
            self.set_status(403, 'missing username argument')

        return self.render()

    @authenticated
    def delete(self, channel_id, username):
        """ remove a user from the chat session"""

        if self.username != None and len(self.username) != 0:

            ## remove our user and alert others in the chat room
            user = self.channel.find_user_by_username(self.username)

            if user != None:
                message = self.get_argument('message', '%s has left the room.' % self.nickname)
                #remove_user(user)

                ## respond to the client our success
                self.set_status(200, 'OK')
                self.delete_cookie('username')
                self.delete_cookie('nickname')
                self.add_to_payload('message',unquote(self.nickname) + ' has left the chat room')

            else:
                ## let the client know we failed because they were not found
                self.set_status(403, 'Authentication failed')

        else:
            ## let the client know we failed because they didn't ask nice
            self.set_status(403, 'missing nickname argument')
        return self.render()

## this allows us to import the demo as a module for unit tests without running the server
##
## runtime configuration
##
project_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

config = {
    'mongrel2_pair': ('ipc://run/mongrel2_send', 'ipc://run/mongrel2_recv'),
    'handler_tuples': [
        (r'^/api/feed/(?P<channel_id>.+)$', FeedHandler),
        (r'^/api/login/(?P<channel_id>.+)/(?P<username>.+)$', ChannelLoginHandler),
        (r'^/api/login/(?P<username>.+)$', LoginHandler),
        (r'^/oauth/twitter$', TwitterOAuthHandler),
        (r'^/oauth/facebook/callback', FacebookOAuthCallbackHandler),
        (r'^/oauth/facebook/login/(?P<username>.+)$', FacebookOAuthRedirectorHandler),
    ],
    'cookie_secret': '_1sRe%%66a^O9s$4c6lq#0F%$9AlH)-6OO1!',
    'enforce_using_redis': False, # This should be true in production 
}


##
## get us started!
##


app = Brubeck(**config)
if using_redis:
    """try to use redis if possible. Only required for persistance across instance restarts and using more than one instance to handle requests.
       Yeah, so you should REALLY use redis in production.
    """
    try:
        ## attach to our redis server
        ## we do all the setup here, so if we fail at anything our flag is set properly right away and we only use in memory buffer from the start            
        redis_server = redis.Redis(host='localhost', port=6379, db=0)

        ## start one client to handle all messages
        ## This could be moved into the ChatChannel if performance becomes an issue
        redis_client1 = redis_server.pubsub()
        redis_client1.subscribe(redis_prefix + 'chat_messages')
        redis_new_chat_messages = redis_client1.listen()

        ## spawn out the process to listen for new messages in redis
        g1 = Greenlet(redis_chat_messages_listener, redis_server)
        g1.start()
        logging.info("started redis listener")

        logging.info("succesfully connected to redis")

    except Exception:
        redis_server = None
        using_redis = False
        logging.info("unable to connect to redis, make sure it is running (single instance mode: using in memory buffer)")

## if we are not using redis, but said we should, stop everything!
if using_redis == False and config.get('enforce_using_redis') == True:
    raise Exception("Y U no use Redis?") 
## spawn out online user checker to timeout users after inactivity
## if there is more than one instance, this will still run in every instance
## that is probably not a very good thing we should deal with eventually
## maybe publish a "last_online_user_check" message allowing each instance to only really process if needed?
g = Greenlet(check_users_online)
g.start()

## start our server to handle requests
if __name__ == "__main__":
    app.run()
