"""Independent speech preferences; secrets are stored only in Keychain."""
import engine
from keychain import operation
from voice import validate_speech_key

VOICES = ('marin', 'cedar', 'coral', 'alloy', 'nova', 'sage', 'shimmer')

def current():
    return dict(engine.STATE.get('voiceSettings', {
        'service': 'openai', 'voice': 'marin', 'hasKey': False}))

def save(body):
    service = body.get('service')
    selected = body.get('voice')
    if service != 'openai' or selected not in VOICES:
        raise ValueError('Choose an available OpenAI voice.')
    key = body.get('key', '')
    if not isinstance(key, str):
        raise ValueError('Enter your voice API key.')
    key = key.strip()
    account = 'scrunch-voice-openai'
    if key:
        validate_speech_key(key)
        operation(account, key)
    elif not operation(account):
        raise ValueError('Paste your OpenAI voice API key first.')
    engine.STATE['voiceSettings'] = {'service': service, 'voice': selected, 'hasKey': True}
    engine.save()
    return current()

TRANSCRIBERS = ('openai', 'on-device', 'gemini')

def set_transcriber(choice):
    """Which service turns his speech into text; OpenAI uses the same Settings → Voice key as spoken replies."""
    if choice not in TRANSCRIBERS:
        raise ValueError('Choose OpenAI, on-device or Gemini for transcription.')
    engine.STATE['voiceEngine'] = choice
    engine.save()
    return {'transcriber': choice}

def config():
    settings = current()
    if not settings.get('hasKey'):
        raise ValueError('Add your voice API key in Settings → Voice.')
    return {'id': 'scrunch-voice-openai', 'kind': 'openai',
            'endpoint': 'https://api.openai.com/v1',
            'ttsModel': 'gpt-4o-mini-tts', 'ttsVoice': settings['voice']}
