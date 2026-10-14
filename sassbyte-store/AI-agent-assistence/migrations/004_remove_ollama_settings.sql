DELETE FROM workspace_settings
WHERE key IN ('ollama_url', 'ollama_model');
