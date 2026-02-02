"""
SMS Memory Agent
A personal assistant that saves links you text it and answers questions about them later.
"""

from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse
from twilio.rest import Client
import anthropic
import sqlite3
import os
import json
import re
from datetime import datetime

# Initialize Flask app
app = Flask(__name__)

# Initialize clients using environment variables
# (We'll set these in Railway)
twilio_client = Client(
    os.environ.get('TWILIO_ACCOUNT_SID'),
    os.environ.get('TWILIO_AUTH_TOKEN')
)
claude_client = anthropic.Anthropic(
    api_key=os.environ.get('ANTHROPIC_API_KEY')
)

TWILIO_PHONE_NUMBER = os.environ.get('TWILIO_PHONE_NUMBER')

# Database setup
def init_db():
    """Create the database table if it doesn't exist."""
    conn = sqlite3.connect('memories.db')
    c = conn.cursor()
    c.execute('''
        CREATE TABLE IF NOT EXISTS items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            category TEXT NOT NULL,
            title TEXT,
            platform TEXT,
            ingredients TEXT,
            location TEXT,
            event_date TEXT,
            caption TEXT,
            original_url TEXT,
            original_message TEXT,
            saved_by TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    conn.commit()
    conn.close()

# Initialize database on startup
init_db()


def classify_and_extract(message_body: str) -> dict:
    """
    Use Claude to classify the message and extract structured info.
    Categories: content, food, events, facts, or query
    """
    
    prompt = f"""You are helping classify and extract information from text messages that users send to save things they find online.

The user sent this message:
"{message_body}"

First, determine if this is:
1. A SAVE request (they're sharing a link or information to save for later)
2. A QUERY request (they're asking a question about things they've saved)

If it's a SAVE request, classify it into one of these categories:
- "content": TV shows, movies, videos, podcasts, music, books, articles to read (extract: title, platform like Netflix/HBO/TikTok/YouTube/Spotify/etc)
- "food": Recipes, restaurants, food ideas (extract: title, ingredients if it's a recipe)
- "events": Events, concerts, exhibitions, things happening at a specific time/place (extract: title, location, event_date)
- "facts": Interesting facts, tips, quotes, information to remember (extract: caption - a summary of the fact)

Respond with JSON only, no other text:

For SAVE requests:
{{"type": "save", "category": "content|food|events|facts", "title": "...", "platform": "...", "ingredients": "...", "location": "...", "event_date": "...", "caption": "..."}}

For QUERY requests:
{{"type": "query", "question": "the user's question"}}

Only include fields relevant to the category. If you can't determine something, use null."""

    response = claude_client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=500,
        messages=[{"role": "user", "content": prompt}]
    )
    
    # Parse the JSON response
    try:
        result = json.loads(response.content[0].text)
        return result
    except json.JSONDecodeError:
        # If Claude didn't return valid JSON, treat as a fact
        return {"type": "save", "category": "facts", "caption": message_body}


def save_item(data: dict, original_message: str, sender: str) -> str:
    """Save an item to the database and return a confirmation message."""
    
    conn = sqlite3.connect('memories.db')
    c = conn.cursor()
    
    # Extract URL from message if present
    url_pattern = r'https?://[^\s]+'
    urls = re.findall(url_pattern, original_message)
    original_url = urls[0] if urls else None
    
    c.execute('''
        INSERT INTO items (category, title, platform, ingredients, location, event_date, caption, original_url, original_message, saved_by)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (
        data.get('category'),
        data.get('title'),
        data.get('platform'),
        data.get('ingredients'),
        data.get('location'),
        data.get('event_date'),
        data.get('caption'),
        original_url,
        original_message,
        sender
    ))
    
    conn.commit()
    conn.close()
    
    # Generate confirmation message
    category = data.get('category')
    title = data.get('title') or data.get('caption', '')[:50]
    
    if category == 'content':
        platform = data.get('platform', 'saved')
        return f"✓ Saved: {title} ({platform})"
    elif category == 'food':
        return f"✓ Saved recipe: {title}"
    elif category == 'events':
        location = data.get('location', '')
        date = data.get('event_date', '')
        return f"✓ Saved event: {title} - {location} {date}".strip()
    else:  # facts
        return f"✓ Saved: {title}..."


def handle_query(question: str) -> str:
    """Search saved items and answer the user's question."""
    
    conn = sqlite3.connect('memories.db')
    c = conn.cursor()
    
    # Get all saved items
    c.execute('SELECT * FROM items ORDER BY created_at DESC')
    rows = c.fetchall()
    conn.close()
    
    if not rows:
        return "You haven't saved anything yet! Text me links to save them."
    
    # Format items for Claude
    columns = ['id', 'category', 'title', 'platform', 'ingredients', 'location', 'event_date', 'caption', 'original_url', 'original_message', 'saved_by', 'created_at']
    items = [dict(zip(columns, row)) for row in rows]
    
    items_text = json.dumps(items, indent=2, default=str)
    
    prompt = f"""You are a helpful assistant. The user has saved various items (content to watch, recipes, events, facts).

Here are all their saved items:
{items_text}

The user is now asking: "{question}"

Give a helpful, concise answer based on their saved items. If they ask what to watch, suggest from their saved content. If they ask what to cook, suggest from their saved recipes. Be conversational and brief (this is a text message).

IMPORTANT: When you recommend something, ALWAYS include the original_url if one exists so the user can open it directly. Format it cleanly.

If nothing matches their question, let them know and suggest what categories they do have saved."""

    response = claude_client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=300,
        messages=[{"role": "user", "content": prompt}]
    )
    
    return response.content[0].text


@app.route('/sms', methods=['POST'])
def handle_sms():
    """Handle incoming SMS messages from Twilio."""
    
    # Get the message details
    message_body = request.form.get('Body', '')
    sender = request.form.get('From', '')
    
    print(f"Received message from {sender}: {message_body}")
    
    # Classify and process the message
    result = classify_and_extract(message_body)
    
    if result.get('type') == 'query':
        response_text = handle_query(result.get('question', message_body))
    else:
        response_text = save_item(result, message_body, sender)
    
    # Send response back via TwiML
    resp = MessagingResponse()
    resp.message(response_text)
    
    return str(resp)


@app.route('/', methods=['GET'])
def health_check():
    """Simple health check endpoint."""
    return "SMS Memory Agent is running!"


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
