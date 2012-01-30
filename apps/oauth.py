#!/usr/bin/env python
from brubeck.auth import authenticated
from brubeck.request_handling import Brubeck, JSONMessageHandler, cookie_encode, cookie_decode
from brubeck.templating import load_jinja2_env, Jinja2Rendering
from dictshield import fields
from dictshield.document import Document
from dictshield.fields import EmbeddedDocument, ShieldException
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
import datetime

from spotichat import find_user_by_username
from spotichat import add_user
from spotichat import User
from spotichat import get_redis_server

from config import ENVIRONMENT
from config import FACEBOOK
from config import TUMBLR
from config import TWITTER

try:
    import redis
except Exception:
    raise Exception("Y U no install Redis?")

def find_user_by_oauth_request_token(token):
    """returns a user by oauth_request_token"""
    if token == None:
        return None;
        
    logging.info("finding oauth_request_token %s in redis " % token)
    key = ENVIRONMENT['REDIS_PREFIX'] + "oauth_request_token:%s" % (token)
    data = get_redis_server().get(key)

    if data != None:
        logging.info("found token (%s):  %s" % (token, data))
        return find_user_by_username(data)
    else:
        logging.info("unable to find user by oauth_request_token (%s): '%s'" % (key, token))
        return None

def add_oauth_request_token(token, username):
    """add an oauth_request_token"""
    try:
        key = "%s" % (token)
    
        logging.info("adding new oauth_request_token: %s (%s)" % (token, username))
    
        # add our user object to a simple set, keyed by username
        key = ENVIRONMENT['REDIS_PREFIX'] + "oauth_request_token:%s" % key
    
        affected = get_redis_server().set(key, username)
        # give us 5 minutes to log in
        get_redis_server().expire(key, 360)
        
        logging.info("added new oauth_request_token (%s): %s" % (affected, key))

    except Exception, e:
        logging.info("error adding oauth_request_token %s: %s" % (token, e))


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
                        self.username = self.get_argument('state', None)
                        if not self.username is None:
                            logging.info("username from state: %s " % (self.username))
                        else:
                            logging.info("No username!")
                        
        except Exception:
            raise

        if self.username is None or len(self.username) == 0:
            self.set_status(403, "username required")

    def _parse_content(self, content):
        """Parses a key value pair or JSON string into a dict"""
        kv_dict = {}
        if content[0] == '{':
            # assume JSON
            kv_dict = json.loads(content)
    
        else:
            kv_dict = sd = dict(u.split('=') for u in content.split('&'))

        return kv_dict  

class ChatifyOAuthRedirector(object):
    """our development oAuth handler"""
    def get(self, settings):
        # we are in development, fake it
        logging.debug( "facebook faking it" );
        user = find_user_by_username(self.username)
        oauth_data = ENVIRONMENT['TEST_OAUTH_DATA']
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


class ChatifyOAuth1aHandler(ChatifyOAuthHandler):
    """Handles oAuth 1.0a authentication"""
    def _signature_base_string(self, http_method, base_uri, query_params, delimiter = "%26"):
        """Creates the base string for an authorized request"""
        query_string = ''
        for key, value in query_params:
            if key != '':
                if query_string != '':
                    query_string = query_string + delimiter
                query_string = query_string + quote( quote( key, '' )  + "=" + quote( value , ''), '' )

        return http_method + "&" + quote(  base_uri, '' ) + "&" + query_string

    def _sign(self, secret_key, base_string ):
        """Creates a HMAC-SHA1 signature"""
        digest = hmac.new(secret_key, base_string, hashlib.sha1).digest()
        return base64.encodestring(digest).rstrip()

    def _authorization_header(self, query_params):
        """build our Authorization header"""
        authorization_header = 'OAuth'
        
        for key, value in query_params:
            if key != '':
                authorization_header = authorization_header + ' ' + key  + '="' + quote( value, '' ) + '",'
        authorization_header = authorization_header.rstrip(',')
        
        return authorization_header
        
    def _generate_nonce(self):
        """generate a nonce"""
        random_number = ''.join(str(random.randint(0, 9)) for i in range(40))
        m = md5.new(str(time.time()) + str(random_number))
        return m.hexdigest()

    def request(self, settings, url, oauth_token, oauth_secret, oauth_verifier):
        """make a signed request"""
        oauth_verifier = self.get_argument('oauth_verifier')
        oauth_timestamp = str(int(time.time()))
        oauth_nonce = self._generate_nonce()
        oauth_consumer_secret = settings['CONSUMER_SECRET']
        oauth_consumer_key = settings['CONSUMER_KEY']

        query_params = [
            ('oauth_consumer_key', oauth_consumer_key),
            ('oauth_nonce', oauth_nonce),
            ('oauth_secret', oauth_secret),
            ('oauth_signature_method', 'HMAC-SHA1'),
            ('oauth_timestamp', oauth_timestamp),
            ('oauth_token', oauth_token),
            ('oauth_verifier', oauth_verifier),
            ('oauth_version', '1.0')
        ]
        signature_base_string = self._signature_base_string('POST', url, query_params)
        signature_key = oauth_consumer_secret + "&" + oauth_secret
        oauth_signature = self._sign(signature_key, signature_base_string)

        query_params = [ 
            ('oauth_consumer_key', oauth_consumer_key),
            ('oauth_nonce', oauth_nonce),
            ('oauth_secret', oauth_secret),
            ('oauth_signature_method', 'HMAC-SHA1'),
            ('oauth_timestamp', oauth_timestamp),
            ('oauth_token', oauth_token),
            ('oauth_verifier', oauth_verifier),
            ('oauth_signature', oauth_signature),
            ('oauth_version', '1.0')
        ]

        authorization_header = self._authorization_header(query_params)
        #print 'Authorization: ' + authorization_header + "\n\n"

        try:
            response = requests.post(url, {}, **{'headers': { 'Authorization': authorization_header } } )

            content = response.content

            #print content;
            
            if content.rfind('&') == -1:
                raise Exception(content);
            kv_pairs = self._parse_content( content );

        except Exception:
            raise

        return kv_pairs

    def callback(self, settings):
        """handle an oAuth 1.0a callback"""

        oauth_token = self.get_argument('oauth_token')
        user = find_user_by_oauth_request_token(oauth_token)
        oauth_data = json.loads(user.oauth_data )
        url = settings['ACCESS_TOKEN_URL']
        oauth_secret = oauth_data['oauth_token_secret']
        oauth_verifier = self.get_argument('oauth_verifier')

        try:
            kv_pairs = self.request(settings, url, oauth_token, oauth_secret, oauth_verifier)
                
        except Exception:
            raise

        if 'oauth_token' in kv_pairs:
            # save our data
            user.oauth_data = json.dumps(kv_pairs)
            
            add_user(user)
        
            return self.redirect("/oauth/" + settings['PROVIDER_NAME']+ "/loggedin")
        else:
            self.set_status(403)
            self.add_to_payload('messages', "Not Authenticated")

        return self.render()

    def redirector(self, settings):
        """ gets the token and redirects the user to the oauth login page """
        url = settings['REQUEST_TOKEN_URL']
        oauth_timestamp = str(int(time.time()))
        oauth_nonce = self._generate_nonce()
        oauth_token = ''
        oauth_secret = ''
        oauth_consumer_secret = settings['CONSUMER_SECRET']
        oauth_callback = settings['CALLBACK_URL']
        oauth_consumer_key = settings['CONSUMER_KEY']

        query_params = [
            ('oauth_callback', oauth_callback ),
            ('oauth_consumer_key', oauth_consumer_key),
            ('oauth_nonce', oauth_nonce),
            ('oauth_secret', oauth_secret),
            ('oauth_signature_method', 'HMAC-SHA1'),
            ('oauth_timestamp', oauth_timestamp),
            ('oauth_token', oauth_token),
            ('oauth_version', '1.0')
        ]
        signature_base_string = self._signature_base_string('POST', url, query_params)
        signature_key = oauth_consumer_secret + "&" + oauth_secret
        oauth_signature = self._sign(signature_key, signature_base_string)

        query_params = [ 
            ('oauth_nonce', oauth_nonce),
            ('oauth_callback', oauth_callback),
            ('oauth_secret', oauth_secret),
            ('oauth_signature_method', 'HMAC-SHA1'),
            ('oauth_timestamp', oauth_timestamp),
            ('oauth_token', oauth_token),
            ('oauth_consumer_key', oauth_consumer_key),
            ('oauth_signature', oauth_signature),
            ('oauth_version', '1.0')
        ]


        #httplib.HTTPConnection.debuglevel = 1
        #opener = urllib2.build_opener(urllib2.HTTPHandler(debuglevel=1))
        #urllib2.install_opener(opener)

        authorization_header = self._authorization_header(query_params)
        print 'Authorization: ' + authorization_header + "\n\n"

        try:
            response = requests.post(url, {}, **{'headers': { 'Authorization': authorization_header } } )

            content = response.content

            print content;
            
            if content.rfind('&') == -1:
                raise Exception(content);
            kv_pairs = self._parse_content( content);

        except Exception:
            raise

        # save our data
        if 'oauth_token' in kv_pairs:
            oauth_token = kv_pairs['oauth_token']

            user = find_user_by_username(self.username)
            logging.debug( "oauth_token %s" % oauth_token );
            if user == None:
                user = User(username=self.username, nickname=self.username, oauth_provider=settings['PROVIDER_NAME'], oauth_data=json.dumps(kv_pairs))
            else:
                user.oauth_provider = settings['PROVIDER_NAME']
                user.oauth_data = json.dumps(kv_pairs)
            
            
            add_user(user)
            add_oauth_request_token( oauth_token, user.username )
            return self.redirect(settings['AUTHORIZE_URL'] + '?oauth_token=' + kv_pairs['oauth_token'])

        # we shouldn't get here
        self.add_to_payload('message', 'an unknown error occured')
        return self.render()

class TumblrOAuthRedirectorHandler(ChatifyOAuth1aHandler):

    def get(self, *args, **kwargs):

        if  ENVIRONMENT['DEBUG'] == True:
            return ChatifyOAuthRedirectorTestHandler.get(self, TUMBLR)

        return self.redirector(TUMBLR);


class TumblrOAuthCallbackHandler(ChatifyOAuth1aHandler):

    def get(self):

        return self.callback(TUMBLR);


class TwitterOAuthCallbackHandler(ChatifyOAuth1aHandler):

    def get(self):

        return self.callback(TWITTER);


class TwitterOAuthRedirectorHandler(ChatifyOAuth1aHandler):

    def get(self, *args, **kwargs):

        if  ENVIRONMENT['DEBUG'] == True:
            return ChatifyOAuthRedirectorTestHandler.get(self, TWITTER)

        return self.redirector(TWITTER);


class FacebookOAuthRedirectorHandler(ChatifyOAuthHandler):
    """Handles Facebook oauth authentication"""

    def get(self, *args, **kwargs):

        self.set_cookie('username', self.username, self.application.cookie_secret, domain="spotichat.com")

        # start the login process
        if  ENVIRONMENT['DEBUG'] == True:
            return ChatifyOAuthRedirectorTestHandler.get(self, FACEBOOK)

        url = "%s?client_id=%s&scope=%s&redirect_uri=%s&display=popup" % (FACEBOOK['REQUEST_URL'], FACEBOOK['APP_ID'], FACEBOOK['SCOPE'], FACEBOOK['REDIRECT_URL'] + "?username=" + self.username)
        # send user to facebook login
        logging.debug( "facebook url %s" % url );
        return self.redirect(url)


class FacebookOAuthCallbackHandler(ChatifyOAuthHandler):
    """Handles Facebook oauth authentication"""

    def get(self):
        code = self.get_argument('code', '')
        # we came from a callback and have our code    
        logging.debug( "facebook callback" );
        url = FACEBOOK['ACCESS_TOKEN_REQUEST_URL']
        fields = { 
            'client_id': FACEBOOK['APP_ID'],
            'redirect_uri': FACEBOOK['REDIRECT_URL'] + "?username=" + self.username,
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
            url =  "%s?access_token=%s" % (FACEBOOK['USER_INFO_URL'], access_token_info['access_token'])
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
