import os, requests
from dotenv import load_dotenv
load_dotenv()
key = os.getenv('DEEPSEEK_API_KEY')
print(f'Key len: {len(key) if key else 0}')
resp = requests.post('https://api.deepseek.com/v1/chat/completions', 
                     headers={'Authorization': f'Bearer {key}'}, 
                     json={'model': 'deepseek-chat', 'messages': [{'role': 'user', 'content': 'hi'}]})
print(f'Status: {resp.status_code}')
print(f'Body: {resp.text}')
