#!/usr/bin/env python3
"""
Test script for rate limiting functionality
This script tests the DOS protection by making multiple requests to the API
"""

import requests
import time
import json
from datetime import datetime
import sys

# Configuration
if len(sys.argv) > 1:
    BASE_URL = sys.argv[1].rstrip('/')
else:
    BASE_URL = "http://127.0.0.1:54321"
TEST_ENDPOINT = "/health"  # Using health endpoint for testing

def test_rate_limiting():
    """Test the rate limiting functionality"""
    print(f"🧪 Testing Rate Limiting System")
    print(f"📍 Target: {BASE_URL}")
    print(f"⏰ Started at: {datetime.now().isoformat()}")
    print("=" * 60)
    
    # Test 1: Normal requests within limit
    print("\n📊 Test 1: Normal requests (should succeed)")
    successful_requests = 0
    failed_requests = 0
    
    for i in range(10):  # Make 10 requests (under the 12/minute limit)
        try:
            response = requests.get(f"{BASE_URL}{TEST_ENDPOINT}", timeout=5)
            if response.status_code == 200:
                successful_requests += 1
                remaining = response.headers.get("X-RateLimit-Remaining", "unknown")
                print(f"  ✅ Request {i+1}: Success (Remaining: {remaining})")
            else:
                failed_requests += 1
                print(f"  ❌ Request {i+1}: Failed with status {response.status_code}")
        except Exception as e:
            failed_requests += 1
            print(f"  ❌ Request {i+1}: Exception - {e}")
        
        time.sleep(0.1)  # Small delay between requests
    
    print(f"\n📈 Results: {successful_requests} successful, {failed_requests} failed")
    
    # Test 2: Exceed rate limit
    print("\n🚨 Test 2: Exceeding rate limit (should get 429 errors)")
    rate_limited_requests = 0
    successful_requests = 0
    
    # Make many requests quickly to trigger rate limiting
    for i in range(20):
        try:
            response = requests.get(f"{BASE_URL}{TEST_ENDPOINT}", timeout=5)
            if response.status_code == 429:
                rate_limited_requests += 1
                error_data = response.json()
                remaining_time = error_data.get("remaining_time", "unknown")
                print(f"  🚫 Request {i+1}: Rate limited (Wait: {remaining_time}s)")
            elif response.status_code == 200:
                successful_requests += 1
                remaining = response.headers.get("X-RateLimit-Remaining", "unknown")
                print(f"  ✅ Request {i+1}: Success (Remaining: {remaining})")
            else:
                print(f"  ❌ Request {i+1}: Unexpected status {response.status_code}")
        except Exception as e:
            print(f"  ❌ Request {i+1}: Exception - {e}")
        
        time.sleep(0.05)  # Very fast requests to trigger rate limiting
    
    print(f"\n📈 Results: {successful_requests} successful, {rate_limited_requests} rate limited")
    
    # Test 3: Wait for rate limit to reset
    print("\n⏳ Test 3: Waiting for rate limit to reset...")
    print("   Waiting 65 seconds for rate limit window to expire...")
    
    for i in range(65, 0, -5):
        print(f"   ⏰ {i} seconds remaining...")
        time.sleep(5)
    
    # Test 4: Verify rate limit has reset
    print("\n🔄 Test 4: Verifying rate limit reset")
    try:
        response = requests.get(f"{BASE_URL}{TEST_ENDPOINT}", timeout=5)
        if response.status_code == 200:
            remaining = response.headers.get("X-RateLimit-Remaining", "unknown")
            print(f"  ✅ Rate limit reset successful (Remaining: {remaining})")
        else:
            print(f"  ❌ Rate limit reset failed (Status: {response.status_code})")
    except Exception as e:
        print(f"  ❌ Rate limit reset test failed: {e}")
    
    # Test 5: Check rate limit status endpoint
    print("\n📊 Test 5: Checking rate limit status endpoint")
    try:
        response = requests.get(f"{BASE_URL}/rate-limit-status", timeout=5)
        if response.status_code == 200:
            status_data = response.json()
            print(f"  📊 Rate Limit Status:")
            print(f"     Client IP: {status_data.get('client_ip')}")
            print(f"     Remaining requests: {status_data.get('remaining_requests')}")
            print(f"     Rate limit: {status_data.get('rate_limit')}")
            print(f"     Window seconds: {status_data.get('window_seconds')}")
            print(f"     Is blocked: {status_data.get('is_blocked')}")
        else:
            print(f"  ❌ Status endpoint failed (Status: {response.status_code})")
    except Exception as e:
        print(f"  ❌ Status endpoint test failed: {e}")

def test_trusted_ips():
    """Test that trusted IPs are exempt from rate limiting"""
    print("\n🔒 Test 6: Trusted IP exemption")
    print("   Note: This test assumes you're running from a trusted IP (127.0.0.1)")
    
    try:
        response = requests.get(f"{BASE_URL}/rate-limit-status", timeout=5)
        if response.status_code == 200:
            status_data = response.json()
            client_ip = status_data.get('client_ip')
            
            if client_ip in ["127.0.0.1", "::1"]:
                print(f"  ✅ Running from trusted IP: {client_ip}")
                print(f"     This IP should be exempt from rate limiting")
            else:
                print(f"  ℹ️  Running from IP: {client_ip}")
                print(f"     Add this IP to TRUSTED_IPS if you want it exempt")
        else:
            print(f"  ❌ Could not check IP status")
    except Exception as e:
        print(f"  ❌ Trusted IP test failed: {e}")

def main():
    """Main test function"""
    print("🚀 UPassport Rate Limiting Test Suite")
    print("=" * 60)
    
    # Check if server is running
    try:
        response = requests.get(f"{BASE_URL}/health", timeout=5)
        if response.status_code == 200:
            print("✅ Server is running and responding")
        else:
            print(f"❌ Server responded with status {response.status_code}")
            return
    except Exception as e:
        print(f"❌ Cannot connect to server: {e}")
        print("   Make sure the UPassport server is running on port 54321")
        return
    
    # Run tests
    test_rate_limiting()
    test_trusted_ips()
    
    print("\n" + "=" * 60)
    print("🏁 Rate limiting tests completed!")
    print("\n📝 Summary:")
    print("   - The system should limit requests to 12 per minute")
    print("   - Trusted IPs (127.0.0.1, ::1) are exempt")
    print("   - Rate limit violations return HTTP 429")
    print("   - Headers show remaining requests and reset time")
    print("   - Automatic cleanup prevents memory leaks")

if __name__ == "__main__":
    main()
