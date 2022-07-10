import os
import logging
from typing import cast, List, Optional, Dict, Union
from functools import lru_cache
from ulauncher.config import EXTENSIONS_DIR, EXT_PREFERENCES_DIR
from ulauncher.utils.db.KeyValueJsonDb import KeyValueJsonDb
from ulauncher.utils.mypy_extensions import TypedDict
from ulauncher.modes.extensions.ExtensionManifest import ExtensionManifest, OptionItem

logger = logging.getLogger()

ValueType = Union[str, int]  # Bool is a subclass of int
PreferenceItem = TypedDict('PreferenceItem', {
    'id': str,
    'type': str,
    'name': str,
    'description': str,
    'default_value': ValueType,
    'min': Optional[int],
    'max': Optional[int],
    'options': List[OptionItem],
    'icon': Optional[str],
    'value': ValueType,
})
PreferenceItems = List[PreferenceItem]


class ExtensionPreferences:
    """
    Manages extension preferences. Stores them as json files in config directory
    """

    manifest = None  # type: ExtensionManifest

    @classmethod
    @lru_cache(maxsize=1000)
    def create_instance(cls, ext_id):
        manifest = ExtensionManifest.new_from_file(f"{EXTENSIONS_DIR}/{ext_id}/manifest.json")
        return cls(ext_id, manifest)

    def __init__(self, ext_id: str, manifest: ExtensionManifest, ext_preferences_dir: str = EXT_PREFERENCES_DIR):
        self.db_path = os.path.join(ext_preferences_dir, f'{ext_id}.json')
        self.db = KeyValueJsonDb[str, str](self.db_path)
        self.manifest = manifest
        self._db_is_open = False

    def get_items(self, type: str = None) -> PreferenceItems:
        """
        :param str type:
        :rtype: list of dicts: [{id: .., type: .., defalut_value: .., value: ..., description}, ...]
        """
        self._open_db()

        items = []  # type: PreferenceItems
        for p in self.manifest.preferences:
            if type and type != p['type']:
                continue

            default_fallback = {'number': 0, 'checkbox': False}.get(p['type'], '')
            default_value = p.get("default_value", default_fallback)
            items.append({
                'id': p['id'],
                'type': p['type'],
                'name': p.get('name', ''),
                'description': p.get('description', ''),
                'min': p.get('min', 0),
                'max': p.get('max', None),
                'options': p.get('options', []),
                'default_value': default_value,
                'value': self.db.find(p['id']) or default_value,
                'icon': p.get('icon', None)
            })

        return items

    def get_dict(self) -> Dict[str, ValueType]:
        """
        :rtype: dict(id=value, id2=value2, ...)
        """
        items = {}
        for i in self.get_items():
            items[i['id']] = i['value']

        return items

    def get(self, id: str) -> Optional[PreferenceItem]:
        """
        Returns one item
        """
        for i in self.get_items():
            if i['id'] == id:
                return i

        return None

    def get_active_keywords(self) -> List[str]:
        """
        Filters items by type "keyword"
        """
        return [cast(str, p['value']) for p in self.get_items(type='keyword') if p['value']]

    def set(self, id: str, value: str):
        """
        Updates preference

        :param str id: id as defined in manifest
        :param str value:
        """
        self.db.put(id, value)
        self.db.commit()

    def _open_db(self):
        if not self._db_is_open:
            self.db.open()
            self._db_is_open = True
