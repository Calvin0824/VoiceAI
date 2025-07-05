from dotenv import load_dotenv
import os

load_dotenv()  # Load from .env

openai_key = os.getenv("OPENAI_API_KEY")
pinecone_key = os.getenv("PINECONE_API_KEY")
pinecone_env = os.getenv("PINECONE_ENV")

print("🔐 Keys loaded:", bool(openai_key), bool(pinecone_key))
