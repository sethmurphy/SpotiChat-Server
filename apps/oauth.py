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
import imp

from spotichat import find_user_by_username
from spotichat import add_user
from spotichat import User
from spotichat import get_redis_server

from config import ENVIRONMENT

try:
    import redis
except Exception:
    raise Exception("Y U no install Redis?")


def find_user_by_oauth_request_token(token):
    """returns a user by oauth_request_token"""
    """oauth_request_token is used to identify the user in the callback"""
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
    """oauth_request_token is set before redirections used to identify the user in the callback"""
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
        logging.info("ERROR adding oauth_request_token %s: %s" % (token, e))


class ChatifyOAuthHandler(JSONMessageHandler):
    """our ChatifyOAuth message handlers base class"""
    """You should never have a route point to it directly, use OAuthHandler"""

    # used to make "static" methods callable without class reference for different oAuth methods(1.0a and 2.0)
    class Callable:
        def __init__(self, anycallable):
            self.__call__ = anycallable

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

    def get_user_info(handler, settings, user, oauth_token):
        """gets additional userinfo after authenticating"""
        """uses USER_INFO from the oauth config file"""

        user_infos = settings['USER_INFO']

        for user_info in user_infos:
            url = user_info[0]
            query_params = {
                'oauth_token': oauth_token
            }
            oauth_version = settings['OAUTH_VERSION']
            if oauth_version == '1.0a':
                kvs = ChatifyOAuth1aHandler._request(handler, 'GET', settings, url, query_params, user)        
            elif oauth_version == '2.0':
                kvs = ChatifyOAuth2Handler._request(handler, 'GET', settings, url, query_params, user)
            else:
                raise Exception("get_user_info: unknow aouth version: %s " % settings['OAUTH_VERSION'])

            if 'response' in kvs:
                ## some providers mave `meta` and `response` wrappers for what is returned
                kvs = kvs['response']

            fields = user_info[1]
            for field in fields:
                value = kvs
                for descriptor in field[1]:
                    value = value[descriptor]
                logging.debug(value) 
                logging.debug(field[0]) 
                kvs.update({ field[0]: value })

        return kvs        

    def _parse_content(self, content):
        """Parses a key value pair or JSON string into a dict"""
        kv_dict = {}
        if content[0] == '{':
            # assume JSON
            kv_dict = json.loads(content)
    
        else:
            kv_dict = dict(u.split('=') for u in content.split('&'))

        return kv_dict  


##################################################
# oAuth 1.0a handler (yucky)
##################################################
class ChatifyOAuth1aHandler(ChatifyOAuthHandler):
    """Handles oAuth 1.0a authentication"""
    """all methods are static"""
    """You should never have a route point to it directly, use OAuthHandler"""

    def _signature_base_string(http_method, base_uri, query_params, delimiter = "%26"):
        """Creates the base string for an authorized request"""
        query_string = ''

        keys = query_params.keys()
        keys.sort()
        
        for param in keys:
            if param != '':
                if query_string != '':
                    query_string = query_string + delimiter
                query_string = query_string + quote( quote( param, '' )  + "=" + quote( query_params[param] , ''), '' )

        return http_method + "&" + quote(  base_uri, '' ) + "&" + query_string

    # make our _signature_base_string method "static"
    _signature_base_string = ChatifyOAuthHandler.Callable(_signature_base_string)

    def _sign(secret_key, base_string ):
        """Creates a HMAC-SHA1 signature"""
        digest = hmac.new(secret_key, base_string, hashlib.sha1).digest()
        return base64.encodestring(digest).rstrip()

    # make our _sign method "static"
    _sign = ChatifyOAuthHandler.Callable(_sign)

    def _authorization_header(query_params):
        """build our Authorization header"""
        authorization_header = 'OAuth'
        
        keys = query_params.keys()
        keys.sort()
        
        for param in keys:
            if param != '':
                authorization_header = authorization_header + ' ' + param  + '="' + quote( query_params[param], '' ) + '",'

        authorization_header = authorization_header.rstrip(',')
        
        return authorization_header

    # make our _authorization_header method "static"
    _authorization_header = ChatifyOAuthHandler.Callable(_authorization_header)

    def _generate_nonce():
        """generate a nonce"""
        random_number = ''.join(str(random.randint(0, 9)) for i in range(40))
        m = md5.new(str(time.time()) + str(random_number))
        return m.hexdigest()

    # make our _generate_nonce method "static"
    _generate_nonce = ChatifyOAuthHandler.Callable(_generate_nonce)

    def _request(handler, http_method, settings, url, params, user):
        """make a signed request for given settings given a url and optional parameters"""
        """The following parameters are not needed in optional:"""
        """oauth_consumer_key,oauth_nonce,oauth_signature_method,"""
        """oauth_timestamp,oauth_version  """

        if user != None:
            oauth_data = json.loads(user.oauth_data )
            oauth_secret = oauth_data['oauth_token_secret']
            params.update({ 'oauth_secret': oauth_secret })
        else:
            oauth_secret = ''

        logging.debug( "_request oauth_secret: %s" % oauth_secret );

        oauth_timestamp = str(int(time.time()))
        oauth_nonce = ChatifyOAuth1aHandler._generate_nonce()
        oauth_consumer_secret = settings['CONSUMER_SECRET']
        oauth_consumer_key = settings['CONSUMER_KEY']

        query_params = {
            'oauth_consumer_key': oauth_consumer_key,
            'oauth_nonce': oauth_nonce,
            'oauth_signature_method': 'HMAC-SHA1',
            'oauth_timestamp': oauth_timestamp,
            'oauth_version': '1.0'
        }
        
        # add optional parameters
        query_params.update(params)

        print query_params

        signature_base_string = ChatifyOAuth1aHandler._signature_base_string(http_method, url, query_params)
        signature_key = oauth_consumer_secret + "&" + oauth_secret
        oauth_signature = ChatifyOAuth1aHandler._sign(signature_key, signature_base_string)

        logging.debug( "signature_base_string: %s" % signature_base_string );
        logging.debug( "signature_key: %s" % signature_key );
        logging.debug( "oauth_signature: %s" % oauth_signature );

        query_params.update({'oauth_signature': oauth_signature});

        authorization_header = ChatifyOAuth1aHandler._authorization_header(query_params)
        #print 'Authorization: ' + authorization_header + "\n\n"

        try:
            if http_method == 'POST':
                response = requests.post(url, {}, **{'headers': { 'Authorization': authorization_header } } )
            else:
                response = requests.get(url, headers = { 'Authorization': authorization_header } )

            content = response.content

            logging.debug( "content: %s" % content );

            if content.rfind('&') == -1 and content.rfind('{') == -1:
                raise Exception(content);

            if content[0:8] == '<DOCTYPE':
                raise Exception("content")

            kv_pairs = handler._parse_content(content);

        except Exception:
            raise

        return kv_pairs

    # make our _request method "static"
    _request = ChatifyOAuthHandler.Callable(_request)

    def redirector(handler, settings):
        """gets the token and redirects the user to the oauth login page """
        """this is always called "statically" from OAuthHandler"""

        try:
            url = settings['REQUEST_TOKEN_URL']
            oauth_callback = settings['CALLBACK_URL']
            
            logging.debug("oauth_callback: %s" % oauth_callback);

            query_params = {
                'oauth_callback': oauth_callback
            }

            kv_pairs = ChatifyOAuth1aHandler._request(handler, 'POST', settings, url, query_params, None)
    
            # save our data
            if 'oauth_token' in kv_pairs:
                oauth_token = kv_pairs['oauth_token']
    
                user = find_user_by_username(handler.username)
                logging.debug( "oauth_token: %s" % oauth_token );
                if user == None:
                    user = User(username=handler.username, nickname=handler.username, current_oauth_provider=settings['PROVIDER_NAME'], oauth_data=json.dumps(kv_pairs))
                else:
                    user.current_oauth_provider = settings['PROVIDER_NAME']
                    user.oauth_data = json.dumps(kv_pairs)


                add_user(user)
                add_oauth_request_token( oauth_token, user.username )

                return handler.redirect(settings['AUTHORIZE_URL'] + '?oauth_token=' + kv_pairs['oauth_token'])

        except Exception:
            raise

        # we shouldn't get here
        handler.add_to_payload('message', 'an unknown error occured')

        return handler.render()

    # make our request method "static"
    redirector = ChatifyOAuthHandler.Callable(redirector)

    def callback(handler, settings):
        """handle an oAuth 1.0a callback"""
        """this is always called "statically" from OAuthHandler"""
        try:

            oauth_token = handler.get_argument('oauth_token')
            oauth_verifier = handler.get_argument('oauth_verifier')

            user = find_user_by_oauth_request_token(oauth_token)

            url = settings['ACCESS_TOKEN_URL']

            logging.debug( "oauth_token: %s" % oauth_token );
            logging.debug( "oauth_verifier: %s" % oauth_verifier );

            query_params = {
                'oauth_token':oauth_token,
                'oauth_verifier':oauth_verifier
            }

            kv_pairs = ChatifyOAuth1aHandler._request(handler, 'POST', settings, url, query_params, user)
            

            if 'oauth_token' in kv_pairs:

                # get our additional user data
                user_infos = settings['USER_INFO'];
                oauth_token = kv_pairs['oauth_token'];

                user.oauth_data = json.dumps(kv_pairs)

                kvs = ChatifyOAuthHandler.get_user_info(handler, settings, user, oauth_token)

                kv_pairs.update(kvs)

                # save our data
                if 'username' in kv_pairs:
                    user.nickname = kv_pairs['username']

                user.oauth_data = json.dumps(kv_pairs)
                
                add_user(user)
            
                return handler.redirect("/oauth/" + settings['PROVIDER_NAME']+ "/loggedin")
            else:
                handler.set_status(403)
                handler.add_to_payload('messages', "Not Authenticated")

        except Exception:
            raise

        return handler.render()

    # make our callback method "static"
    callback = ChatifyOAuthHandler.Callable(callback)


##################################################
# oAuth 2.0 handler
##################################################
class ChatifyOAuth2Handler(ChatifyOAuthHandler):
    """Handles oAuth 2.0 authentication"""
    """all methods are static"""
    """You should never have a route point to it directly, use OAuthHandler"""


    def _request(handler, http_method, settings, url, params, user):
        """make a signed request for given settings given a url and optional parameters"""
        """The following parameters are not needed in optional:"""
        """oauth_consumer_key,oauth_nonce,oauth_signature_method,"""
        """oauth_timestamp,oauth_version  """

        try:
            if http_method == 'POST':
                response = requests.post(url, params)
            else:
                response = requests.get(url, params = params)

            content = response.content

            logging.debug( "content: %s" % content );

            if content.rfind('&') == -1 and content.rfind('{') == -1:
                raise Exception(content);

            kv_pairs = handler._parse_content(content);

        except Exception:
            raise

        return kv_pairs

    # make our _request method "static"
    _request = ChatifyOAuthHandler.Callable(_request)

    def redirector(handler, settings):
        """handle the redirect to an oauth provider"""
        """this is always called "statically" from OAuthHandler"""
        oauth_request_token = handler.username

        user = find_user_by_username(handler.username)
        if user == None:
            user = User(username=handler.username, nickname=handler.username, current_oauth_provider=settings['PROVIDER_NAME'])
        else:
            user.current_oauth_provider = settings['PROVIDER_NAME']

        add_user(user)
        add_oauth_request_token( oauth_request_token, user.username )

        url = "%s?client_id=%s&scope=%s&display=popup&redirect_uri=%s" % (settings['REQUEST_URL'], settings['APP_ID'], settings['SCOPE'], settings['REDIRECT_URL'] + "?oauth_request_token=" + oauth_request_token)

        # send user to facebook login
        logging.debug( settings['PROVIDER_NAME'] + " url %s" % url );
        return handler.redirect(url)

    # make our redirector method "static"
    redirector = ChatifyOAuthHandler.Callable(redirector)

    def callback(handler, settings):
        """handle the callback from an oauth provider"""
        """this is always called "statically" from OAuthHandler"""
        # we came from a callback and have our oauth_request_token
        oauth_request_token = handler.get_argument('oauth_request_token')

        # if succesfull in getting token, get some about me info
        user = find_user_by_oauth_request_token(oauth_request_token)
        
        logging.debug( "facebook callback: %s" % oauth_request_token );
        
        url = settings['ACCESS_TOKEN_REQUEST_URL']
        query_params = { 
            'client_id': settings['APP_ID'],
            'redirect_uri': settings['REDIRECT_URL'] + "?oauth_request_token=" + oauth_request_token,
            'client_secret': settings['APP_SECRET'],
            'code': handler.get_argument('code')
        }
        
        kv_pairs = ChatifyOAuth2Handler._request(handler, "POST", settings, url, query_params, user)


        if 'access_token' in kv_pairs:
            access_token = kv_pairs['access_token']
            logging.debug( "access_token %s" % access_token );
            # get a little more data about the user (me query)

            kvs = ChatifyOAuthHandler.get_user_info(handler, settings, user, access_token)

            kv_pairs.update(kvs)

            oauth_data = json.dumps(kv_pairs)

            # we should store this data now
            if user == None:
                user = User(username=handler.username, nickname=kv_pairs['username'], current_oauth_provider=settings['PROVIDER_NAME'], oauth_data=oauth_data)
            else:
                user.nickname = kv_pairs['username']
                user.oauth_id = kv_pairs['id']
                user.current_oauth_provider = settings['PROVIDER_NAME']
                user.oauth_data = oauth_data

            logging.debug( "oauth_data: %s" % oauth_data );

            # adding an existing key just replaces it
            add_user(user)

            return handler.redirect("/oauth/" + settings['PROVIDER_NAME'] + "/loggedin")
        else:
            handler.set_status(403)
            handler.add_to_payload('messages', "Not Authenticated")

        return handler.render()

    # make our redirector method "static"
    callback = ChatifyOAuthHandler.Callable(callback)

##################################################
# Test handler 
##################################################
class OAuthRedirectorTestHandler(object):
    """our development oAuth handler"""
    """parses a predifined callback from [provider].oauth.config.py"""
    def get(self, settings):
        # we are in development, fake it
        logging.debug( "facebook faking it" );
        user = find_user_by_username(self.username)
        oauth_data = ENVIRONMENT['TEST_OAUTH_DATA']
        # we should store this data now
        if user == None:
            user = User(username=self.username, nickname=oauth_data['username'], current_oauth_provider='facebook', oauth_data=json.dumps(oauth_data))
        else:
            user.nickname = oauth_data['username']
            user.current_oauth_provider = 'facebook'
            user.oauth_data = json.dumps(oauth_data)
        
        # adding an existing key just replaces it
        add_user(user)

        return self.redirect("/oauth/facebook/loggedin")

###########################################
# The handlers we actualy use for routing
###########################################

class OAuthHandler(ChatifyOAuthHandler):
    """oauth routing handler. All requests come through here. If not first, eventually."""
    def get(self, provider, action, username):

        project_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

        file = project_dir + '/apps/' + provider + '.oauth.config.py'
        logging.debug ('loading %s oAuth settings from %s' % (provider, file))
        oauth_settings = imp.load_source('auth_settings', file)
        settings = oauth_settings.Settings
        if  ENVIRONMENT['DEBUG'] == True:
            return OAuthRedirectorTestHandler.get(self, settings)

        if settings['OAUTH_VERSION'] == '1.0a':
            if action == 'login':
                return ChatifyOAuth1aHandler.redirector(self, settings);

            if action == 'callback':
                return ChatifyOAuth1aHandler.callback(self, settings);

        if settings['OAUTH_VERSION'] == '2.0':
            if action == 'login':
                return ChatifyOAuth2Handler.redirector(self, settings);

            if action == 'callback':
                return ChatifyOAuth2Handler.callback(self, settings);

        self.set_status(403)
        self.add_to_payload('messages', "Unsupported oauth provider")
         
        return self.render();


class OAuthCallbackHandler(OAuthHandler):
    """a wrapper around OAuthHandler handler to deal with having no username parameter from the routing rule"""
    def get(self, provider, action):
        return OAuthHandler.get(self, provider, action, self.username)
