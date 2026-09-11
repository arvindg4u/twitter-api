> [!NOTE]
> https://github.com/d60/twitter_login (under development)

## MCP Server (use from any AI agent)

This repo also serves a **production MCP server** (Streamable HTTP, stateless)
at `/mcp` with 13 tools: `search_tweets`, `search_users`, `get_user`,
`get_user_tweets`, `get_tweet`, `get_trends` (public) plus `post_tweet`,
`like_tweet`, `unlike_tweet`, `retweet`, `unretweet`, `delete_tweet`,
`send_dm` (need `api_key`).

**Claude Code** (`mcp add`, Streamable HTTP):
```bash
claude mcp add --transport http twitter-api https://twitter-api-gjyc.onrender.com/mcp/
```

**Cursor / Claude Desktop** (`mcp.json`):
```json
{
  "mcpServers": {
    "twitter-api": {
      "url": "https://twitter-api-gjyc.onrender.com/mcp/"
    }
  }
}
```

**Local dev** (stdio): `python twitter_mcp.py` · **Inspector**: point it at the URL above.
Write tools pass `api_key` (the service `API_KEY`). Full REST API also available
(see `app.py`). Auth cookies: `POST /cookies`, `COOKIES_JSON` env, or
`POST /bootstrap-login` (headless Chromium).

<img src="https://i.imgur.com/iJe6rsZ.png"  width="500">



![Number of GitHub stars](https://img.shields.io/github/stars/d60/twikit)
![GitHub commit activity](https://img.shields.io/github/commit-activity/m/d60/twikit)
![Version](https://img.shields.io/pypi/v/twikit?label=PyPI)
[![Tweet](https://img.shields.io/twitter/url/http/shields.io.svg?style=social)](https://twitter.com/intent/tweet?text=Create%20your%20own%20Twitter%20bot%20for%20free%20with%20%22Twikit%22!%20%23python%20%23twitter%20%23twikit%20%23programming%20%23github%20%23bot&url=https%3A%2F%2Fgithub.com%2Fd60%2Ftwikit)
[![Discord](https://img.shields.io/badge/Discord-%235865F2.svg?style=for-the-badge&logo=discord&logoColor=white)](https://discord.gg/nCrByrr8cX)
[![BuyMeACoffee](https://img.shields.io/badge/-buy_me_a%C2%A0coffee-gray?logo=buy-me-a-coffee)](https://www.buymeacoffee.com/d60py)

[[日本語](https://github.com/d60/twikit/blob/main/README-ja.md)]
[[中文](https://github.com/d60/twikit/blob/main/README-zh.md)]


# Twikit <img height="35"  src="https://i.imgur.com/9HSdIl4.png"  valign="bottom">

A Simple Twitter API Scraper

You can use functions such as posting or searching for tweets without an API key using this library.

- [Documentation (English)](https://twikit.readthedocs.io/en/latest/twikit.html)


🔵 [Discord](https://discord.gg/nCrByrr8cX)

> [!NOTE]
> Released twikit_grok an extension for using Grok AI with Twikit.  
> For more details, visit: https://github.com/d60/twikit_grok.




## Features

### No API Key Required

This library uses scraping and does not require an API key.

### Free

This library is free to use.


## Functionality

By using Twikit, you can access functionalities such as the following:

-  Create tweets

-  Search tweets

-  Retrieve trending topics

- etc...



## Installing

```bash

pip install twikit

```



## Quick Example

**Define a client and log in to the account.**

```python
import asyncio
from twikit import Client

USERNAME = 'example_user'
EMAIL = 'email@example.com'
PASSWORD = 'password0000'

# Initialize client
client = Client('en-US')

async def main():
    await client.login(
        auth_info_1=USERNAME,
        auth_info_2=EMAIL,
        password=PASSWORD,
        cookies_file='cookies.json'
    )

asyncio.run(main())
```

**Create a tweet with media attached.**

```python
# Upload media files and obtain media_ids
media_ids = [
    await client.upload_media('media1.jpg'),
    await client.upload_media('media2.jpg')
]

# Create a tweet with the provided text and attached media
await client.create_tweet(
    text='Example Tweet',
    media_ids=media_ids
)

```

**Search the latest tweets based on a keyword**
```python
tweets = await client.search_tweet('python', 'Latest')

for tweet in tweets:
    print(
        tweet.user.name,
        tweet.text,
        tweet.created_at
    )
```

**Retrieve user tweets**
```python
tweets = await client.get_user_tweets('123456', 'Tweets')

for tweet in tweets:
    print(tweet.text)
```

**Send a dm**
```python
await client.send_dm('123456789', 'Hello')
```

**Get trends**
```python
await client.get_trends('trending')
```

More Examples: [examples](https://github.com/d60/twikit/tree/main/examples) <br>

## Contributing

If you encounter any bugs or issues, please report them on [issues](https://github.com/d60/twikit/issues).


If you find this library useful, consider starring this repository⭐️
