# Telegram File Bot (Python)

Bot က admin ပို့တဲ့ ဖိုင်တွေကို deeplink ထုတ်ပြီး channel မှာ post တင်ပေးပါတယ်။

## Features

- File/Video ပို့ရင် → Deeplink ထုတ်ပေး
- File name ထဲက link တွေ ဖျက်ပေး
- Forwarded message ကနေ "Forwarded from" ဖျက်ပြီး post တင်ပေး
- Channel မှာ movie post အနေနဲ့ တင်ပေး

## Setup

1. BotFather မှာ bot ဖန်တီးပြီး token ယူပါ
2. `pip install -r requirements.txt` run ပါ
3. `.env` file ဖန်တီးပြီး variables ထည့်ပါ
4. `python main.py` run ပါ

## Environment Variables

- `BOT_TOKEN` - Telegram bot token
- `ADMIN_ID` - Admin ရဲ့ Telegram user ID
- `CHANNEL_ID` - Channel username (e.g., @mychannel)

## Deploy to Render

1. GitHub မှာ repo ဖန်တီးပါ
2. Render မှာ Web Service ဖန်တီးပါ
3. Build Command: `pip install -r requirements.txt`
4. Start Command: `python main.py`
5. Environment Variables ထည့်ပါ
6. Deploy ပါ

## Bot Commands

- `/start` - Bot start
- ဖိုင်ပို့ - Deeplink ထုတ်ပေး
- Forward ပြီးပို့ - Channel မှာ post တင်ပေး
