import bcrypt

# 1. Define your string
my_string = "your_secret_string"

# 2. Generate a salt and hash the string
# The string must be encoded to bytes first
hashed_bytes = bcrypt.hashpw(my_string.encode('utf-8'), bcrypt.gensalt())

# 3. Decode to string and slice the last 15 characters
last_15 = hashed_bytes.decode('utf-8')[-15:]

print(f"Stored portion: {last_15}")
