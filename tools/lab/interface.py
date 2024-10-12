from flask import Flask, render_template
import os
import json

app = Flask(__name__)

@app.route('/')
def index():
    emails = os.listdir('./emails')
    return render_template('index.html', emails=emails)

@app.route('/context/<email>')
def show_context(email):
    context_file = os.path.join('./emails', email, 'context.txt')
    embeddings_file = os.path.join('./emails', email, 'embeddings.json')
    
    context = ""
    if os.path.exists(context_file):
        with open(context_file, 'r') as f:
            context = f.read()
    
    embeddings = []
    if os.path.exists(embeddings_file):
        with open(embeddings_file, 'r') as f:
            embeddings = json.load(f)
    
    return render_template('context.html', email=email, context=context, embeddings=embeddings)

if __name__ == '__main__':
    app.run(debug=True)

