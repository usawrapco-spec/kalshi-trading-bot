FROM python:3.11-slim

# Set working directory
WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application files
COPY . .

# Create data and logs directories
RUN mkdir -p data logs

# Make scripts executable
RUN chmod +x bot.py setup.sh

# Run the bot
CMD ["python", "-u", "bot.py"]
