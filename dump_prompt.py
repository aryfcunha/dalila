import os, requests
from dalila.pipeline import get_brief_data
from dalila.config import get_config
from dalila.db import connect
from datetime import date
from dotenv import load_dotenv

load_dotenv()
cfg = get_config()
day = date(2026, 5, 14)
with connect() as conn:
    data = get_brief_data(conn, day)
    
editor_prompt = (cfg.prompts_dir / 'editor.md').read_text()
user_p = str(data) # Simplified
key = os.getenv('DEEPSEEK_API_KEY')

print(f'Attempting with real content len: {len(editor_prompt) + len(user_p)}')
resp = requests.post('https://api.deepseek.com/v1/chat/completions', 
                     headers={'Authorization': f'Bearer {key}'}, 
                     json={'model': 'deepseek-chat', 'messages': [{'role': 'user', 'content': f'{editor_prompt}\n\n{user_p}'}]})
print(f'Status: {resp.status_code}')
print(f'Body: {resp.text}')
