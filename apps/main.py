#!/usr/bin/env python
from brubeck.auth import authenticated
from brubeck.request_handling import Brubeck, JSONMessageHandler
from brubeck.templating import load_jinja2_env, Jinja2Rendering
from dictshield import fields
from dictshield.document import Document
from dictshield.fields import EmbeddedDocument, ShieldException
from gevent import Greenlet
from gevent.event import Event
from urllib import unquote
import functools
import logging
import os
import time

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

def get_chat_channel(channel_name):
    """finds or creates a new ChatChannel"""
    if channel_name not in chat_channels:
        chat_channel = ChatChannel(channel_name, redis_server)
        chat_channels[channel_name] = chat_channel
        logging.info("Created chat channel with channel name %s" % (chat_channel.name))
    else:
        chat_channel = chat_channels[channel_name]
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

    def __init__(self, name, redis_server = None, buffer_size = BUFFER_SIZE):
        self.channel_id = name
        self.name = name
        ## this is how we alert our member of new stuff
        self.new_message_event = Event()

        ## hold our messages in memory here, limit to last 50
        ## persistance is handled by redis, or not at all
        self.chat_messages = []
        self.users_online = []
        self.buffer_size = BUFFER_SIZE;
        self._load_buffer()
        self.redis_server = redis_server
        self.using_redis = (redis_server != None)

    def _load_buffer(self):
        try:
            ## fill the in memory buffer with redis data here
            key = "chat_messages:%s" % self.channel_id
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
        """adds a user to the redis server and publishes it"""

        data = user.to_json()
        key = "%s:%s" % (self.channel_id, user.nickname)

        logging.info("adding new user timestamp: %s" % key)
        # add our nickname to a set orderes by timestamp to be able to quickly purge
        affected = self.redis_server.zadd(redis_prefix + "users_timestamp",key, user.timestamp)
        logging.info("added new user timestamp(%s): %s:%s" % (affected, key, user.timestamp))
        # add our user object to a simple set, keyed by nickname
        key = redis_prefix + "users:%s" % key

        affected = self.redis_server.set(key, data)
        logging.info("added new user (%s): %s" % (affected, key))

        ## we no longeer care about updating users information in this or other chatify instances
        # publish our new user

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
            if self.users_online[i].nickname == user.nickname:
                del self.users_online[i]
                break

    def redis_remove_user(self, user):
        """removes a user from the redis server and publishes it"""

	data = user.to_json()
        key = "%s:%s" % (self.channel_id, user.nickname)

        logging.info(data)
        # remove our users timestamp
        affected = self.redis_server.zrem(redis_prefix + 'users_timestamp',key)
        logging.info("removed user timestamp(%d): %s" % (affected, key))
        # remove our user 
        key = redis_prefix + "users:%s" % (key)
        affected = self.redis_server.expire(key, 0)
        logging.info("removed user(%d): %s" % (affected, key))

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
        usr = find_user_by_nickname(user.nickname)
        if usr != None:
            usr.timestamp = user.timestamp

        return usr

    def redis_update_user_timestamp(self, user):
        """timestamps our active user and publishes the changes"""
        data = user.to_json()
        key = "%s:%s" % (self.channel_id, user.nickname)

        logging.info("updating users timestamp: %s" % key)
        # update our timestamp ordered set
        
        affected = self.redis_server.zadd(redis_prefix + "users_timestamp", key, user.timestamp)

        return user

    def find_user_by_nickname(self, nickname):
        """returns a user by nickname, trying redis first"""
        if self.using_redis:
            user = self.redis_find_user_by_nickname(nickname)
        else:
            user = self.list_find_user_by_nickname(nickname)
        return user

    def list_find_user_by_nickname(self, nickname):
        """returns the first list item matching a nickname"""
        logging.info("finding %s in user list " % nickname)
        users = filter(lambda x: x.nickname == nickname,
                       self.users_online)
        if len(users)==0:
            return None
        else:
            return users[0]

    def redis_find_user_by_nickname(self, nickname):
        """returns the user by nickname"""
        logging.info("finding %s in redis " % nickname)
        key = redis_prefix + "users:%s:%s" % (self.channel_id, nickname)
        data = self.redis_server.get(key)

        if data != None:
            logging.info("found user by nickname (%s):  %s" % (key, data))
            return User(**json.loads(data))
        else:
            logging.info("unable to find user by nickname (%s): '%s'" % (key, nickname))
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
            msg = ChatMessage(nickname='system', message="%s can not been found in the room %s" % (user.nickname, chat_chanel.channel_name), channel_name = chat_channel.name);

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
                nickname = key.split(':')[1]
                key = redis_prefix + "users:%s" % key
                data = redis_server.get(key)
                if data != None:
                    user = User(**json.loads(data))

                    msg = ChatMessage(nickname='system', message="%s can not been found in the room" % user.nickname, channel_name = channel_name);
                    
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
    nickname = fields.StringField(required=True, max_length=40)

    def __init__(self, *args, **kwargs):
        super(User, self).__init__(*args, **kwargs)
        
        # seconds is enough here, we need an int
        self.timestamp = int(time.time())

class ChatMessage(EmbeddedDocument):
    """A single chat message"""
    timestamp = fields.IntField(required=True)
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
        nickname = None
        try:
            self.channel_id = self.get_argument('channel_id', 'public')

            if hasattr(self, '_url_args') and len(self._url_args) > 0:
                self.channel_id=  unquote(self._url_args[0]).decode('utf-8')
                self.channel = get_chat_channel(self.channel_id)

            if hasattr(self, '_url_args') and len(self._url_args) > 1:
                self.nickname = unquote(self._url_args[1]).decode('utf-8')
            else:
                self.nickname = self.get_argument('nickname', None)

            if self.nickname != None:
                self.nickname = unescape(self.nickname)
                user = self.channel.find_user_by_nickname(self.nickname)
                if user != None:
                    self.current_user = self.channel.update_user_timestamp(user)

        except Exception:
            pass

        self.headers['Access-Control-Allow-Origin'] = '*'
        self.headers['Access-Control-Allow-Headers'] = 'Origin, Content-Type, User-Agent, Accept, Cache-Control, Pragma'
        self.headers['Access-Control-Allow-Methods'] = 'POST, GET, DELETE, OPTIONS'


    def get_current_user(self):
        """return  self.current_user set in self.prepare()"""
        return self.current_user


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
        logging.info("%s: %s" % (self.nickname, message))
        msg = ChatMessage(**{'nickname': self.nickname, 'message': message,
                             'channel_name': self.channel_id})

        try:
            msg.validate()
            self.channel.add_chat_message(msg)

            self.set_status(200);
            self.add_to_payload('message','message sent')

        except ShieldException, se:
            self.set_status(403, 'VALIDATION ERROR: %s' % (se));

        return self.render()

class LoginHandler(ChatifyJSONMessageHandler):
    """Allows users to enter the chat room.  Does no authentication."""

    def post(self, channel_id, nickname):
        message = self.get_argument('message', '%s has entered the room.' % nickname)

        if self.nickname != None and len(self.nickname) != 0:
            user = self.channel.find_user_by_nickname(self.nickname)

            if user == None :
                logging.info("logging in user %s to %s" % (self.nickname, self.channel_id))
                user = self.channel.add_user(User(nickname=self.nickname))
                msg = ChatMessage(timestamp=int(time.time() * 1000), nickname='system',
                    message=message, msgtype='system', channel_name=self.channel_id)

                self.channel.add_chat_message(msg)

                ## respond to the client our success
                self.set_status(200)
                self.set_cookie('nickname', self.nickname.encode('utf-8'))
                self.add_to_payload('message', self.nickname + ' has entered the chat room')

            else:
                ## let the client know we failed because they didn't ask nice
                self.set_status(403, 'identity theft is a serious crime')

        else:
            ## let the client know we failed because they didn't ask nice
            self.set_status(403, 'missing nickname argument')

        self.convert_cookies()
        return self.render()

    def delete(self, channel_id, nickname):
        """ remove a user from the chat session"""
        message = self.get_argument('message', '%s has left the room.' % self.nickname)

        if self.nickname != None and len(self.nickname) != 0:

            ## remove our user and alert others in the chat room
            user = self.channel.find_user_by_nickname(self.nickname)

            if user != None:
                self.channel.remove_user(user)
                msg = ChatMessage(timestamp=int(time.time() * 1000), nickname='system',
                   message=message, msgtype='system', channel_name=self.channel_id)

                self.channel.add_chat_message(msg)

                ## respond to the client our success
                self.set_status(200)
                self.set_cookie('nickname', None)
                self.add_to_payload('message',unquote(self.nickname) + ' has left the chat room')

            else:
                ## let the client know we failed because they were not found
                self.set_status(403, 'Authentication failed')

        else:
            ## let the client know we failed because they didn't ask nice
            self.set_status(403, 'missing nickname argument')
        self.convert_cookies()
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
        (r'^/api/login/(?P<channel_id>.+)/(?P<nickname>.+)$', LoginHandler),
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
