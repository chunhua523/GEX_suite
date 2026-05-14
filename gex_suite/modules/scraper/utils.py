import logging
import os
import re
import time
from datetime import datetime

def setup_logging(log_widget=None):
    """
    Sets up logging configuration.
    If log_widget is provided, it should have a .write() method (like a Text widget wrapper).
    """
    logger = logging.getLogger("LietaScraper")
    logger.setLevel(logging.INFO)
    
    formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s', datefmt='%H:%M:%S')

    # Console Handler
    ch = logging.StreamHandler()
    ch.setFormatter(formatter)
    logger.addHandler(ch)

    return logger

def get_timestamp_filename(prefix="data", extension=".txt"):
    """
    Returns a filename with current timestamp.
    e.g. data_20241025_120000.txt
    """
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return f"{prefix}_{timestamp}{extension}"

def clean_filename(filename):
    """
    Sanitizes a string to be safe for filenames.
    """
    return re.sub(r'[<>:"/\\|?*]', '_', filename)

def load_tickers_from_file(filepath):
    """
    Reads tickers from a file (txt or csv). 
    Assumes one ticker per line or comma separated.
    For backward compatibility, flattens groups into a single list.
    """
    groups = load_tickers_with_groups(filepath)
    # Flatten all groups into a single list
    tickers = []
    for group_name, ticker_list in groups.items():
        tickers.extend(ticker_list)
    return tickers

def load_tickers_with_groups(filepath):
    """
    Reads tickers with groups from a JSON file.
    Returns a dict where keys are group names and values are lists of tickers.
    
    Format: {"Group1": ["TICK1", "TICK2"], "Group2": ["TICK3"]}
    
    If the file is in old plain text format, it will be automatically migrated.
    """
    import json
    
    if not os.path.exists(filepath):
        return {"Default": []}

    # Google Drive CloudStorage paths can briefly raise Errno 11 (EDEADLK) while
    # the desktop client syncs the file. Retry a few times before giving up.
    content = None
    last_err = None
    for attempt in range(5):
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                content = f.read().strip()
            break
        except OSError as e:
            last_err = e
            if attempt < 4:
                time.sleep(2 ** attempt)  # 1s, 2s, 4s, 8s
    if content is None:
        print(f"Error loading ticker file {filepath}: {last_err} (after 5 retries)")
        return {"Default": []}

    try:
            
        # Try to parse as JSON first
        try:
            data = json.loads(content)
            
            # Fast path: Quick validation for well-formed data
            if isinstance(data, dict) and data:
                # Quick check: if all values are lists, it's likely valid
                # Only do deep validation if we detect potential issues
                all_lists = all(isinstance(v, list) for v in data.values())
                
                if all_lists:
                    # Quick sample check: look at first value of first group
                    first_group = next(iter(data.values()), [])
                    
                    # If empty or first item is a plain string (not JSON-like), assume valid
                    if not first_group or (isinstance(first_group[0], str) and 
                                          not first_group[0].strip().startswith(('{', '"', '['))):
                        return data
                
                # Potential corruption detected - do deep validation
                is_corrupted = False
                for group_name, ticker_list in data.items():
                    if isinstance(ticker_list, list):
                        # Check if list contains JSON-like strings
                        if any(isinstance(item, str) and 
                              (item.strip().startswith('{') or 
                               item.strip().startswith('\\"') or 
                               item.strip() in ['{', '}', '[', ']']) for item in ticker_list):
                            is_corrupted = True
                            break
                    else:
                        raise ValueError(f"Group '{group_name}' value must be a list")
                
                if is_corrupted:
                    print(f"⚠️ Detected corrupted double-encoded JSON in {filepath}")
                    print("   Attempting to recover data...")
                    
                    # Try to extract ticker data from corrupted structure
                    recovered_data = {}
                    current_group = None
                    current_tickers = []
                    
                    for key, value in data.items():
                        if isinstance(value, list):
                            for item in value:
                                item_str = str(item).strip()
                                
                                # Skip structural JSON characters
                                if item_str in ['{', '}', '[', ']', ',']:
                                    continue
                                
                                # Detect group definition: "GroupName": [
                                if '": [' in item_str or '\": [' in item_str:
                                    # Save previous group
                                    if current_group and current_tickers:
                                        recovered_data[current_group] = current_tickers
                                    
                                    # Extract new group name
                                    group_match = item_str.strip('", ').replace('": [', '').replace('\": [', '').strip('"\\"')
                                    current_group = group_match
                                    current_tickers = []
                                
                                # Detect ticker (quoted string that's not a group definition)
                                elif item_str.startswith('\\"') and item_str.endswith('\\"'):
                                    ticker = item_str.strip('"\\"')
                                    if ticker and not ticker.endswith(':'):
                                        current_tickers.append(ticker)
                    
                    # Save last group
                    if current_group and current_tickers:
                        recovered_data[current_group] = current_tickers
                    
                    if recovered_data:
                        print(f"✓ Successfully recovered {len(recovered_data)} groups")
                        # Save the recovered data
                        save_tickers_with_groups(filepath, recovered_data)
                        return recovered_data
                    else:
                        raise ValueError("Could not recover data from corrupted file")
                
                # Valid structure, return it
                return data
            elif not isinstance(data, dict):
                raise ValueError("JSON root must be a dictionary")
            else:
                # Empty dict
                return data
            
        except (json.JSONDecodeError, ValueError) as e:
            # If it's a corrupted structure error, re-raise it
            if "corrupted" in str(e).lower() or "Could not recover" in str(e):
                raise
            
            # Otherwise, treat as legacy plain text format - migrate it
            print(f"Migrating legacy format: {filepath}")
            parts = re.split(r'[,\n]+', content)
            tickers = [p.strip() for p in parts if p.strip()]
            migrated_data = {"Default": tickers}
            
            # Auto-save migrated format
            save_tickers_with_groups(filepath, migrated_data)
            
            return migrated_data
    except Exception as e:
        print(f"Error loading ticker file {filepath}: {e}")
        return {"Default": []}

def save_tickers_with_groups(filepath, groups_dict):
    """
    Saves tickers with groups to a JSON file.
    
    Args:
        filepath: Path to save the file
        groups_dict: Dict with group names as keys and ticker lists as values
                     Example: {"Tech": ["AAPL", "MSFT"], "Finance": ["JPM"]}
    """
    import json
    
    try:
        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(groups_dict, f, indent=2, ensure_ascii=False)
        return True
    except Exception as e:
        print(f"Error saving ticker file {filepath}: {e}")
        return False

def get_ticker_group(filepath, ticker):
    """
    Returns the group name that contains the given ticker.
    Returns None if ticker is not found.
    """
    groups = load_tickers_with_groups(filepath)
    for group_name, ticker_list in groups.items():
        if ticker in ticker_list:
            return group_name
    return None
