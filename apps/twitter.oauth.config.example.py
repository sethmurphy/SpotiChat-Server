Settings = {
    "PROVIDER_NAME": "twitter",
    "OAUTH_VERSION": "1.0a",
    "CONSUMER_KEY": "[YOUR VALUE HERE]",
    "CONSUMER_SECRET": "[YOUR VALUE HERE]",
    "REQUEST_TOKEN_URL": "https://api.twitter.com/oauth/request_token",
    "REQUEST_TOKEN_URL_HOST": "https://api.twitter.com",
    "REQUEST_TOKEN_URL_PATH": "/oauth/request_token",
    "AUTHORIZE_URL": "https://api.twitter.com/oauth/authorize",
    "ACCESS_TOKEN_URL": "https://api.twitter.com/oauth/access_token",
    "CALLBACK_URL": "http://[YOUR DOMAIN HERE]/oauth/twitter/callback",
    "USER_INFO": [  
        ["https://api.twitter.com/1/account/verify_credentials.json", 
            [
                ["username", ["screen_name"]], 
                ["oauth_id", ["id"]]
            ],
        ],
    ],
    "TEST_OAUTH_DATA": { 
        "id": "[YOUR VALUE HERE]",
        "screen_name": "[YOUR VALUE HERE]"
    },    
}

