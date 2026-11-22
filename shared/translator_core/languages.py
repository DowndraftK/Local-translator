"""Language identities shared by the Mac workspaces, Vision and model routes."""
from . import CoreError

LANGUAGES = {
    'en': ('英语', 'English', 'en-US'),
    'zh': ('简体中文', 'Simplified Chinese', 'zh-Hans'),
    'ja': ('日语', 'Japanese', 'ja-JP'),
    'ru': ('俄语', 'Russian', 'ru-RU'),
    'ko': ('韩语', 'Korean', 'ko-KR'),
    'es': ('西班牙语', 'Spanish', 'es-ES'),
    'pt': ('葡萄牙语', 'Portuguese', 'pt-BR'),
}
DEFAULT_DIRECTION = 'en-zh'
LEGACY_DIRECTIONS = ('en-zh', 'zh-en')


def direction_pair(direction):
    if isinstance(direction, str):
        pair = direction.split('-')
        if len(pair) == 2 and pair[0] != pair[1] and all(code in LANGUAGES for code in pair):
            return tuple(pair)
    raise CoreError('请选择不同的原文语言和译文语言。', 'direction_unsupported')


def valid_direction(direction):
    try:
        direction_pair(direction)
        return True
    except CoreError:
        return False


def direction_label(direction):
    left, right = direction_pair(direction)
    return LANGUAGES[left][0] + ' → ' + LANGUAGES[right][0]


def vision_languages(direction):
    source, _ = direction_pair(direction)
    language = LANGUAGES[source][2]
    if source in ('en', 'zh'):
        return [language, LANGUAGES['zh' if source == 'en' else 'en'][2]]
    return [language]


def speech_language(variant, direction):
    source, _ = direction_pair(direction)
    if variant == 'small.en' and source != 'en':
        raise CoreError('Whisper small.en 仅支持英语原文。请选择多语 Whisper 模型，或将原文语言改为英语。',
                        'asr_language_unsupported')
    if variant not in ('small.en', 'large-v3-turbo'):
        raise CoreError('请在偏好设置导入并检查兼容的 Whisper 模型。', 'audio_binding')
    return source
