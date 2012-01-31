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
import random
import json
import datetime
try:
    import redis
except Exception:
    raise Exception("Y U no install Redis?")

from config import ENVIRONMENT


## holds all our ChatChannel objects
chat_channels = {}

redis_server = None

def get_redis_server():
    """get our Redis server connection"""
    return redis_server

def set_redis_server(server):
    """set our Redis server connection"""
    redis_server = server

def find_user_by_username(username):
    """returns a user by nickname"""
    if username == None:
        return None;
        
    logging.info("finding user %s in redis " % username)
    key = ENVIRONMENT['REDIS_PREFIX'] + "users:%s" % (username)
    data = redis_server.get(key)

    if data != None:
        logging.info("found user by username (%s):  %s" % (key, data))
        return User(**json.loads(data))
    else:
        logging.info("unable to find user by username (%s): '%s'" % (key, username))
        return None

##
## Method to add a user to the chat server
## We never remove them, that would be impolite
##

def add_user(user):
    """add a user to our users."""
    try:
        data = user.to_json()
        key = "%s" % (user.username)
    
        logging.info("adding new user timestamp: %s" % key)
    
        # add our user object to a simple set, keyed by username
        key = ENVIRONMENT['REDIS_PREFIX'] + "users:%s" % key
    
        affected = redis_server.set(key, data)
        logging.info("added new user (%s): %s" % (affected, key))

    except Exception, e:
        logging.info("ERROR adding user %s: %s" % (user, e))


def get_chat_channel(redis_server, channel_id):
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

def redis_chat_messages_listener(redis_server, redis_new_chat_messages):
    """listen to redis for when new messages are published"""
    logging.info("Spun up a redis chat message listener.")
    while True:
        raw = redis_new_chat_messages.next()
        msg = (ChatMessage(**json.loads(raw['data'])))
        ## just hook into our existing way for now
        ## a bit redundant but allows server to be run without redis
        logging.info("new chat message subscribed to: %s" % raw['data'])
        ## add o our local buffer to push to clients
        chat_channel = get_chat_channel(redis_server, msg.channel_name)
        chat_channel.list_add_chat_message(msg)

#
# Our channel definition class
#

class ChatChannel(object):
    """encapsulates a chat channel"""

    def __init__(self, channel_id, redis_server = None, buffer_size = ENVIRONMENT['BUFFER_SIZE']):
        self.channel_id = channel_id
        self.name = channel_id
        ## this is how we alert our member of new stuff
        self.new_message_event = Event()

        ## hold our messages in memory here, limit to last 50
        ## persistance is handled by redis, or not at all
        self.chat_messages = []
        self.buffer_size = ENVIRONMENT['BUFFER_SIZE'];
        self._load_buffer()
        self.redis_server = redis_server

    def _load_buffer(self):
        try:
            ## fill the in memory buffer with redis data here
            key = ENVIRONMENT['REDIS_PREFIX'] + "chat_messages:%s" % self.channel_id
            msgs = redis_server.lrange(key, -1 * self.buffer_size, -1)
            i = 0
            for msg in msgs:
                try:
                    message = ChatMessage(**json.loads(msg))
                    self.chat_messages.append(message)
                    i += 1
                except Exception, e:
                    logging.info("ERROR loading message %s: %s" % (msg, e))

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
        """adds a message to the redis server and publishes it"""
        try:
            data = message.to_json()
            key = ENVIRONMENT['REDIS_PREFIX'] + "chat_messages:%s" % self.channel_id
    
            logging.info(data)
    
            self.redis_server.rpush(key, data)
            self.redis_server.publish(ENVIRONMENT['REDIS_PREFIX'] + 'chat_messages', data)
        except Exception, e:
            logging.info("ERROR adding message %s: %s" % (message, e))
            raise

    def list_add_chat_message(self, chat_message):
        """Adds a message to our message history. A server timestamp is used to
        avoid sending duplicates."""
        self.chat_messages.append(chat_message)

        #logging.info("adding message: %s" % chat_message.message)

        if len(self.chat_messages) > ENVIRONMENT['BUFFER_SIZE']:
            self.chat_messages.pop(0)

        # alert our polling clients
        self.new_message_event.set()
        self.new_message_event.clear()

    def get_messages(self, since_timestamp=0):
        """get new messages since a certain timestamp"""
        return filter(lambda x: x.timestamp > since_timestamp,
                      self.chat_messages)

    ##
    ## Methods to add a user to the channel
    ##

    def add_user(self, user):
        """adds a user to the redis server and publishes it"""

        try:
            logging.info("channel redis add_user")
    
            data = user.to_json()
            key = "%s:%s" % (self.channel_id, user.username)
    
            logging.info("adding new user timestamp: %s" % key)
            # add our username to a set orderes by timestamp to be able to quickly purge
            affected = self.redis_server.zadd(ENVIRONMENT['REDIS_PREFIX'] + "users_timestamp",key, user.timestamp)
            logging.info("added new user timestamp(%s): %s:%s" % (affected, key, user.timestamp))
        except Exception, e:
            logging.info("ERROR adding user %s: %s" % (user, e))

    ##
    ## Methods to remove a user
    ##
    def remove_user(self, user):
        """removes a user from the redis server and publishes it"""

        data = user.to_json()
        key = "%s:%s" % (self.channel_id, user.username)

        logging.info(data)
        # remove our users timestamp
        affected = self.redis_server.zrem(ENVIRONMENT['REDIS_PREFIX'] + 'users_timestamp',key)
        logging.info("removed user timestamp(%d): %s" % (affected, key))


    ##
    ## Update our users timestamp methods
    ##

    def update_user_timestamp(self, user):
        """timestamps our active user and publishes the changes"""
        data = user.to_json()
        key = "%s:%s" % (self.channel_id, user.username)

        logging.info("updating users timestamp: %s" % key)
        # update our timestamp ordered set
        
        affected = self.redis_server.zadd(ENVIRONMENT['REDIS_PREFIX'] + "users_timestamp", key, user.timestamp)

        return user

    def find_user_by_username(self, username):
        """returns the user by username"""
        user = None
        logging.info("channel finding %s in redis " % username)
        key = "%s:%s" % (self.channel_id, username)
        # see if we have a timestamp in the room
        rank = self.redis_server.zrank(ENVIRONMENT['REDIS_PREFIX'] + "users_timestamp", key)
        logging.info("channel %s users_timestamp rank (%s): %s " % (ENVIRONMENT['REDIS_PREFIX'], key, rank))
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

def check_users_online(redis_server):
    """check for expired users and send a message they left the room"""
    before_timestamp = int((time.time()) - (ENVIRONMENT['USER_TIMEOUT']))

    logging.info("checking users online, purging before %s" % before_timestamp)
    logging.info("checking for users before: %s" % before_timestamp)    

    expired_users_count = redis_server.zcount(ENVIRONMENT['REDIS_PREFIX'] + "users_timestamp",0,before_timestamp)
    logging.info("found %d users to expire" % expired_users_count)
    if expired_users_count > 0:
        expired_users = redis_server.zrange(ENVIRONMENT['REDIS_PREFIX'] + "users_timestamp",0, expired_users_count)
        if expired_users != None:
            for key in expired_users:
                channel_name = key.split(':')[0]
                username = key.split(':')[1]
                key = ENVIRONMENT['REDIS_PREFIX'] + "users:%s" % username
                data = redis_server.get(key)
                if data != None:
                    user = User(**json.loads(data))

                    msg = ChatMessage(nickname='system', username='system', message="%s can not been found in the room" % user.nickname, channel_name = channel_name);
                    
                    chat_channel = get_chat_channel(redis_server, channel_name)
                    chat_channel.add_chat_message(msg)
                    chat_channel.remove_user(user)
                else:
                    logging.info("unable to find expired user: %s" % (key))


    ## setup our next check
    g = Greenlet(check_users_online, redis_server)
    g.start_later(ENVIRONMENT['USER_TIMEOUT_INTERVAL'])



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
    current_oauth_provider = fields.StringField(max_length=40)
    oauth_data = fields.StringField(required=True, max_length=2048)

    def __init__(self, *args, **kwargs):
        super(User, self).__init__(*args, **kwargs)
        
        # seconds is enough here, we need an int
        self.timestamp = int(time.time())

class ChatMessage(EmbeddedDocument):
    """A single chat message"""
    timestamp = fields.IntField(required=True)
    username = fields.StringField(required=True, max_length=50)
    nickname = fields.StringField(required=True, max_length=50)
    message = fields.StringField(required=True)
    msgtype = fields.StringField(default='user',
                          choices=['user', 'error', 'system'])
    channel_name = fields.StringField(required=True, max_length=255)

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

            self.channel = get_chat_channel(redis_server, self.channel_id)

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
            self.channel.wait(ENVIRONMENT['POLLING_INTERVAL'])

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

