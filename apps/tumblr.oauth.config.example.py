Settings = {
    "PROVIDER_NAME": "tumblr",
    "OAUTH_VERSION": "1.0a",
    "CONSUMER_KEY": "[YOUR VALUE HERE]",
    "CONSUMER_SECRET": "[YOUR VALUE HERE]",
    "REQUEST_TOKEN_URL": "http://www.tumblr.com/oauth/request_token",
    "REQUEST_TOKEN_URL_HOST": "http://www.tumblr.com",
    "REQUEST_TOKEN_URL_PATH": "/oauth/request_token",
    "AUTHORIZE_URL": "http://www.tumblr.com/oauth/authorize",
    "ACCESS_TOKEN_URL": "http://www.tumblr.com/oauth/access_token",
    "CALLBACK_URL": "http://[YOUR DOMAIN HERE]/oauth/tumblr/callback",
    "USER_INFO": [  
        ["http://api.tumblr.com/v2/user/info", 
            [
                ["username", ["user","name"]], 
                ["oauth_id", ["user","name"]]
            ],
        ],
    ],
    "TEST_OAUTH_DATA": { 
        "user": {"name":"[YOUR VALUE HERE]"}},
    },
}