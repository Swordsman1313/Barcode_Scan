#!/usr/bin/env bash
# ==============================================================================
# Telegram Stock Count Bot - Quick Setup Script
# ==============================================================================

set -e

echo "🚀 Setting up Telegram Stock Count Bot environment..."

# 1. Check Python version
if ! command -v python3 &> /dev/null; then
    echo "❌ Error: python3 is not installed. Please install Python 3.10+."
    exit 1
fi

echo "✅ Python found: $(python3 --version)"

# 2. Create Virtual Environment
if [ ! -d "venv" ]; then
    echo "📦 Creating virtual environment..."
    python3 -m venv venv
else
    echo "📦 Virtual environment already exists."
fi

# 3. Activate and install dependencies
echo "📥 Installing dependencies..."
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

# 4. Create required directories
mkdir -p photos

# 5. Create .env if not present
if [ ! -f ".env" ]; then
    echo "⚙️ Creating .env file from template..."
    cp .env.example .env
    echo "⚠️ Please edit .env and insert your TELEGRAM_BOT_TOKEN!"
else
    echo "⚙️ .env file already exists."
fi

# 6. Run test suite
echo "🧪 Running system verification tests..."
python3 -m unittest tests/test_suite.py

echo ""
echo "======================================================================"
echo "🎉 Setup complete! You're ready to start."
echo ""
echo "1. Edit .env with your Bot Token:"
echo "   nano .env"
echo ""
echo "2. Start the Bot:"
echo "   source venv/bin/activate"
echo "   python3 bot.py"
echo "======================================================================"
