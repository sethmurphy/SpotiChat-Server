Settings = {
    "PROVIDER_NAME": "facebook",
    "OAUTH_VERSION": "2.0",
    "APP_ID": "[YOUR VALUE HERE]",
    "APP_SECRET": "[YOUR VALUE HERE]",
    "SCOPE": "user_about_me",
    "REDIRECT_URL": "http://[YOUR DOMAIN HERE]/oauth/facebook/callback",
    "REQUEST_URL": "https://www.facebook.com/dialog/oauth",
    "ACCESS_TOKEN_REQUEST_URL": "https://graph.facebook.com/oauth/access_token",
    "USER_INFO": [  
        ["https://graph.facebook.com/me", 
            [
                ["username", ["username"]], 
                ["oauth_id", ["id"]]
            ],
        ],
    ],
    "TEST_OAUTH_DATA": { 
        "id": "[YOUR VALUE HERE]",
        "username": "[YOUR VALUE HERE]"
    },
}
