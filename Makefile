.PHONY: install run test clean

install:
	pip install -r requirements.txt

run:
	python3 54321.py

test:
	python3 test_rate_limit_aggressive.py

clean:
	find . -type d -name "__pycache__" -exec rm -rf {} +
	find . -type f -name "*.pyc" -delete
