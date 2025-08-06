def get_config_value(config, section, key, default):
    """
    Safely gets a value from the config, removing inline comments and handling type casting.
    The type of the default value determines the return type.

    Args:
        config (ConfigParser): The configuration object.
        section (str): The section of the config to read from.
        key (str): The key of the value to read.
        default: The default value to use if the key is not found or parsing fails.
                 The type of this default value is used for casting.

    Returns:
        The parsed configuration value, or the default if not found or on error.
    """
    # Get the raw value from the config, with the default as a fallback
    raw_value = config.get(section, key, fallback=str(default))

    if not isinstance(raw_value, str):
        # Should not happen with ConfigParser's fallback behavior, but as a safeguard.
        return raw_value

    # Clean the value by removing inline comments (anything after a '#') and stripping whitespace
    clean_value = raw_value.split('#')[0].strip()

    try:
        # Determine the target type from the default value provided
        target_type = type(default)

        if target_type == bool:
            # Handle boolean conversion for common string representations
            return clean_value.lower() in ['true', 'yes', '1', 'on', 'enabled']
        elif target_type == int:
            return int(clean_value)
        elif target_type == float:
            return float(clean_value)
        else:
            # For strings or other types, return the cleaned string
            return clean_value

    except (ValueError, TypeError):
        # If casting fails for any reason, return the original default value
        return default
