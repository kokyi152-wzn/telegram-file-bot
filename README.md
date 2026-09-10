# Telegram File Bot (Python)

Bot က admin ပို့တဲ့ ဖိုင်တွေကို deeplink ထုတ်ပြီး channel မှာ post တင်ပေးပါတယ်။ Deeplink ကိုနှိပ်တဲ့ လူတိုင်း ဖိုင်ကို ပြန်ရရှိနိုင်ပါတယ်။

## Features

- File/Video ပို့ရင် → Deeplink ထုတ်ပေးပြီး MongoDB မှာသိမ်း
- Deeplink click လုပ်တဲ့သူကို ဖိုင်ပြန်ပို့ပေး
- File name ထဲက link တွေ ဖျက်ပေး
- Forwarded message ကနေ "Forwarded from" ဖျက်ပြီး ပြန်တင်ပေး
- Source channel ပျက်သွားရင်လည်း မပျောက်ရအောင် file ကို download ပြီး bot ရဲ့ ကိုယ်ပိုင်အဖြစ်ပြန် upload
- Channel မှာ movie post အနေနဲ့ တင်ပေး

## Setup

1. BotFather မှာ bot ဖန်တီးပြီး token ယူပါ
2. MongoDB Atlas free cluster ဆောက်ပြီး connection string ယူပါ
3. `pip install -r requirements.txt` run ပါ
4. `.env` file ဖန်တီးပြီး variables ထည့်ပါ
5. `python main.py` run ပါ

## Environment Variables

- `BOT_TOKEN` - Telegram bot token
- `ADMIN_ID` - Admin ရဲ့ Telegram user ID
- `CHANNEL_ID` - Channel username (e.g., @mychannel)
- `MONGODB_URI` - MongoDB Atlas connection string (secret! GitHub မှာ မတင်ပါနဲ့)
- `MONGO_DB_NAME` - Database name (optional, default: `telegram_bot`)

## Deploy to Koyeb

1. GitHub မှာ public repo ဖန်တီးပြီး code push ပါ
2. Koyeb မှာ Service ဖန်တီးပြီး GitHub repo ချိတ်ပါ (Dockerfile ရှိလို့ command တွေ အလွတ်ထားလို့ရတယ်)
3. Environment Variables ထည့်ပါ
4. Deploy ပါ

## Bot Flow

- **ဖိုင်/Video ပို့** → Deeplink ထုတ်ပေး (admin ထဲက message မှာ ပေါ်တယ်)
- **Forward ပြီးပို့** → Channel မှာ movie post တင်ပြီး Deeplink ထုတ်ပေး
- **Deeplink နှိပ်တဲ့သူ** → Bot က ဖိုင်ကို ပြန်ပို့ပေးတယ်

## Bot Commands

- `/start` - Bot start
- `/start <id>` - Deeplink ကနေ ဖိုင်ရယူ