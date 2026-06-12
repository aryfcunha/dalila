import os, requests
from dotenv import load_dotenv
load_dotenv()
key = os.getenv('DEEPSEEK_API_KEY')
sys_p = 'You are a neutral analyst. Summarize news.' * 100
user_p = 'Here is a news item: Sudan conflict escalates.' * 100
resp = requests.post('https://api.deepseek.com/v1/chat/completions', 
                     headers={'Authorization': f'Bearer {key}'}, 
                     json={'model': 'deepseek-chat', 'messages': [{'role': 'user', 'content': f'{sys_p}\n\n{user_p}'}]})
print(f'Status: {resp.status_code}')
print(f'Body: {resp.text}')
