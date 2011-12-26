#!/usr/bin/env python
from urllib import unquote, quote
import sys
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
import urllib3

from config import TWITTER

logging.basicConfig(level=logging.DEBUG)
"""Handles twitter ouath authentication"""
def _signature_base_string(http_method, base_uri, query_params, delimiter = "%26"):
    """Creates the base string ro an authorized twitter request"""
    query_string = ''
    for key, value in query_params:
        if query_string != '':
            query_string = query_string + delimiter
        if value != '' and key != '':
            query_string = query_string + quote( key , '') + "%3D" + quote( value, '' )

    return http_method + "&" + quote(  base_uri, '' ) + "&" + query_string

def _sign(secret_key, base_string ):
    """Creates a HMAC-SHA1 signature"""
    digest = hmac.new(secret_key, base_string, hashlib.sha1).digest()
    return base64.encodestring(digest).rstrip()

def _authorization_header(query_params):
    authorization_header = 'OAuth '
    
    for key, value in query_params:
        #print key
        #print value
        if value != '':
            authorization_header = authorization_header + key  + '="' + quote( value, '' ) + '", '
    authorization_header = authorization_header.rstrip(', ')
    
    return authorization_header
    
def _generate_nonce():
    random_number = ''.join(str(random.randint(0, 9)) for i in range(40))
    m = md5.new(str(time.time()) + str(random_number))
    return m.hexdigest()


url = TWITTER['REQUEST_TOKEN_URL']
time_stamp = str(int(time.mktime(time.gmtime())))
#time_stamp = str( int( time.time() ) )
nonce = _generate_nonce()
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
signature_base_string = _signature_base_string('GET', url, query_params)
signature_key = TWITTER['CONSUMER_SECRET'] + "&" + oauth_secret;
signature = _sign(signature_key, signature_base_string)

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

authorization_header = _authorization_header(query_params)
print authorization_header + "\n\n"

#req = urllib2.Request(url, {},)
#req.add_header('Authorization', authorization_header);

the_page = "error"

try:
    #response = urllib2.urlopen(req)
    #response = opener.open(req)
    #the_page = response.read()
    
    #response = requests.post(url, {}, **{'headers': { 'Authorization': authorization_header } } )
    #the_page = response.content
    print url + "\n\n"
    pool = urllib3.HTTPConnectionPool("api.twitter.com")    
    response = pool.request('GET', url, fields = {}, headers = { 'Authorization': authorization_header } )
    the_page = response.data
    
    #print response.history
    #print response.headers
except Exception:
    raise
#print req.headers
print the_page + "\n\n"


    
