#!/usr/bin/env python
# -*- coding: utf-8 -*-
from __future__ import absolute_import, division, unicode_literals
from logging import getLogger
import urllib
import copy

from . import nodes
from ..plex_db import PlexDB
from ..plex_api import API
from .. import kodi_db
from .. import itemtypes, path_ops
from .. import plex_functions as PF, music, utils, variables as v, app
from ..utils import etree

LOG = getLogger('PLEX.sync.sections')

BATCH_SIZE = 500
SECTIONS = []
# Need a way to interrupt our synching process
IS_CANCELED = None

LIBRARY_PATH = path_ops.translate_path('special://profile/library/video/')
# The video library might not yet exist for this user - create it
if not path_ops.exists(LIBRARY_PATH):
    path_ops.copy_tree(
        src=path_ops.translate_path('special://xbmc/system/library/video'),
        dst=LIBRARY_PATH,
        preserve_mode=0)  # dont copy permission bits so we have write access!
PLAYLISTS_PATH = path_ops.translate_path("special://profile/playlists/video/")
if not path_ops.exists(PLAYLISTS_PATH):
    path_ops.makedirs(PLAYLISTS_PATH)

# Windows variables we set for each node
WINDOW_ARGS = ('index', 'title', 'id', 'path', 'type', 'content', 'artwork')


class Section(object):
    """
    Setting the attribute section_type will automatically set content and
    sync_to_kodi
    """
    def __init__(self, index=None, xml_element=None, section_db_element=None):
        # Unique Plex id of this Plex library section
        self._section_id = None  # int
        # Building block for window variable
        self._node = None  # unicode
        # Index of this section (as section_id might not be subsequent)
        # This follows 1:1 the sequence in with the PMS returns the sections
        self._index = None  # Codacy-bug
        self.index = index  # int
        # This section's name for the user to display
        self.name = None  # unicode
        # Library type section (NOT the same as the KODI_TYPE_...)
        # E.g. 'movies', 'tvshows', 'episodes'
        self.content = None  # unicode
        # Setting the section_type WILL re_set sync_to_kodi!
        self._section_type = None  # unicode
        # Do we sync all items of this section to the Kodi DB?
        # This will be set with section_type!!
        self.sync_to_kodi = None  # bool
        # For sections to be synched, the section name will be recorded as a
        # tag. This is the corresponding id for this tag
        self.kodi_tagid = None  # int
        # When was this section last successfully/completely synched to the
        # Kodi database?
        self.last_sync = None  # int
        # Path to the Kodi userdata library FOLDER for this section
        self._path = None  # unicode
        # Path to the smart playlist for this section
        self._playlist_path = None
        # "Poster" for this section
        self.icon = None  # unicode
        # Background image for this section
        self.artwork = None
        # Thumbnail for this section, similar for each section type
        self.thumb = None
        # Order number in which xmls will be listed inside Kodei
        self.order = None
        # Original PMS xml for this section, including children
        self.xml = None
        # Attributes that will be initialized later by full_sync.py
        self.iterator = None
        self.context = None
        self.get_children = None
        # A section_type encompasses possible several plex_types! E.g. shows
        # contain shows, seasons, episodes
        self.plex_type = None
        if xml_element is not None:
            self.from_xml(xml_element)
        elif section_db_element:
            self.from_db_element(section_db_element)

    def __repr__(self):
        return ("{{"
                "'index': {self.index}, "
                "'name': '{self.name}', "
                "'section_id': {self.section_id}, "
                "'section_type': '{self.section_type}', "
                "'sync_to_kodi': {self.sync_to_kodi}, "
                "'last_sync': {self.last_sync}"
                "}}").format(self=self)
    __str__ = __repr__

    def __nonzero__(self):
        return (self.section_id is not None and
                self.name is not None and
                self.section_type is not None)

    def __eq__(self, section):
        return (self.section_id == section.section_id and
                self.name == section.name and
                self.section_type == section.section_type)
    __ne__ = not __eq__

    @property
    def section_id(self):
        return self._section_id

    @section_id.setter
    def section_id(self, value):
        self._section_id = value
        self._path = path_ops.path.join(LIBRARY_PATH, 'Plex-%s' % value, '')
        self._playlist_path = path_ops.path.join(PLAYLISTS_PATH,
                                                 'Plex %s.xsp' % value)

    @property
    def section_type(self):
        return self._section_type

    @section_type.setter
    def section_type(self, value):
        self._section_type = value
        self.content = v.MEDIATYPE_FROM_PLEX_TYPE[value]
        # Default values whether we sync or not based on the Plex type
        if value == v.PLEX_TYPE_PHOTO:
            self.sync_to_kodi = False
        elif not app.SYNC.enable_music and value == v.PLEX_TYPE_ARTIST:
            self.sync_to_kodi = False
        else:
            self.sync_to_kodi = True

    @property
    def index(self):
        return self._index

    @index.setter
    def index(self, value):
        self._index = value
        self._node = 'Plex.nodes.%s' % value

    @property
    def node(self):
        return self._node

    @property
    def path(self):
        return self._path

    @property
    def playlist_path(self):
        return self._playlist_path

    def from_db_element(self, section_db_element):
        self.section_id = section_db_element['section_id']
        self.name = section_db_element['section_name']
        self.section_type = section_db_element['plex_type']
        self.kodi_tagid = section_db_element['kodi_tagid']
        self.sync_to_kodi = section_db_element['sync_to_kodi']
        self.last_sync = section_db_element['last_sync']

    def from_xml(self, xml_element):
        """
        Reads section from a PMS xml (Plex id, name, Plex type)
        """
        api = API(xml_element)
        self.section_id = utils.cast(int, xml_element.get('key'))
        self.name = api.title()
        self.section_type = api.plex_type()
        self.icon = api.one_artwork('composite')
        self.artwork = api.one_artwork('art')
        self.thumb = api.one_artwork('thumb')
        self.xml = xml_element

    def from_plex_db(self, section_id, plexdb=None):
        """
        Reads section with id section_id from the plex.db
        """
        if plexdb:
            section = plexdb.section(section_id)
        else:
            with PlexDB(lock=False) as plexdb:
                section = plexdb.section(section_id)
        if section:
            self.from_db_element(section)

    def to_plex_db(self, plexdb=None):
        """
        Writes this Section to the plex.db, potentially overwriting
        (INSERT OR REPLACE)
        """
        if not self:
            raise RuntimeError('Section not clearly defined: %s' % self)
        if plexdb:
            plexdb.add_section(self.section_id,
                               self.name,
                               self.section_type,
                               self.kodi_tagid,
                               self.sync_to_kodi,
                               self.last_sync)
        else:
            with PlexDB(lock=False) as plexdb:
                plexdb.add_section(self.section_id,
                                   self.name,
                                   self.section_type,
                                   self.kodi_tagid,
                                   self.sync_to_kodi,
                                   self.last_sync)

    def addon_path(self, args):
        """
        Returns the plugin path pointing back to PKC for key in order to browse
        args is a dict. Its values may contain string info of the form
            {key: '{self.<Section attribute>}'}
        """
        args = copy.deepcopy(args)
        for key, value in args.iteritems():
            args[key] = value.format(self=self)
        return 'plugin://plugin.video.plexkodiconnect?%s' % urllib.urlencode(args)

    def to_kodi(self):
        """
        Writes this section's nodes to the library folder in the Kodi userdata
        directory
        Won't do anything if self.sync_to_kodi is not True
        """
        if self.index is None:
            raise RuntimeError('Index not initialized')
        # Main list entry for this section - which will show the different
        # nodes as "submenus" once the user navigates into this section
        args = {
            'mode': 'browseplex',
            'key': '/library/sections/%s' % self.section_id,
            'plex_type': self.section_type,
            'section_id': unicode(self.section_id)
        }
        if not self.sync_to_kodi:
            args['synched'] = 'false'
        addon_index = self.addon_path(args)
        if self.sync_to_kodi and self.section_type in v.PLEX_VIDEOTYPES:
            path = 'library://video/Plex-{0}/{0}_all.xml'
            path = path.format(self.section_id)
            index = 'library://video/Plex-%s' % self.section_id
        else:
            # No xmls to link to - let's show the listings on the fly
            index = addon_index
            args['key'] = '/library/sections/%s/all' % self.section_id
            path = self.addon_path(args)
        # .index will list all possible nodes for this library
        utils.window('%s.index' % self.node, value=index)
        utils.window('%s.title' % self.node, value=self.name)
        utils.window('%s.type' % self.node, value=self.content)
        utils.window('%s.content' % self.node, value=path)
        # .path leads to all elements of this library
        utils.window('%s.path' % self.node,
                     value='ActivateWindow(Videos,%s,return)' % path)
        utils.window('%s.id' % self.node, value=str(self.section_id))
        # To let the user navigate into this node when selecting widgets
        utils.window('%s.addon_index' % self.node, value=addon_index)
        if not self.sync_to_kodi:
            self.remove_files_from_kodi()
            return
        if self.section_type == v.PLEX_TYPE_ARTIST:
            # Todo: Write window variables for music
            return
        if self.section_type == v.PLEX_TYPE_PHOTO:
            # Todo: Write window variables for photos
            return

        # Create a dedicated directory for this section
        if not path_ops.exists(self.path):
            path_ops.makedirs(self.path)
        # Create a tag just like the section name in the Kodi DB
        with kodi_db.KodiVideoDB(lock=False) as kodidb:
            self.kodi_tagid = kodidb.create_tag(self.name)
        # The xmls are numbered in order of appearance
        self.order = 0
        if not path_ops.exists(path_ops.path.join(self.path, 'index.xml')):
            LOG.debug('Creating index.xml for section %s', self.name)
            xml = etree.Element('node',
                                attrib={'order': unicode(self.order)})
            etree.SubElement(xml, 'label').text = self.name
            etree.SubElement(xml, 'icon').text = self.icon or nodes.ICON_PATH
            self._write_xml(xml, 'index.xml')
        self.order += 1
        # Create the one smart playlist for this section
        if not path_ops.exists(self.playlist_path):
            self._write_playlist()
        # Now build all nodes for this section - potentially creating xmls
        for node in nodes.NODE_TYPES[self.section_type]:
            self._build_node(*node)

    def _build_node(self, node_type, node_name, args, content, pms_node):
        self.content = content
        node_name = node_name.format(self=self)
        xml_name = '%s_%s.xml' % (self.section_id, node_type)
        path = path_ops.path.join(self.path, xml_name)
        if not path_ops.exists(path):
            if pms_node:
                # Even the xml will point back to the PKC add-on
                xml = nodes.node_pms(self, node_name, args)
            else:
                # Let's use Kodi's logic to sort/filter the Kodi library
                xml = getattr(nodes, 'node_%s' % node_type)(self, node_name)
            self._write_xml(xml, xml_name)
        self.order += 1
        self._window_node(path, node_name, node_type, pms_node)

    def _write_xml(self, xml, xml_name):
        LOG.debug('Creating xml for section %s: %s', self.name, xml_name)
        utils.indent(xml)
        etree.ElementTree(xml).write(path_ops.path.join(self.path, xml_name),
                                     encoding='utf-8',
                                     xml_declaration=True)

    def _write_playlist(self):
        LOG.debug('Creating smart playlist for section %s: %s',
                  self.name, self.playlist_path)
        xml = etree.Element('smartplaylist',
                            attrib={'type': v.MEDIATYPE_FROM_PLEX_TYPE[self.section_type]})
        etree.SubElement(xml, 'name').text = self.name
        etree.SubElement(xml, 'match').text = 'all'
        rule = etree.SubElement(xml, 'rule', attrib={'field': 'tag',
                                                     'operator': 'is'})
        etree.SubElement(rule, 'value').text = self.name
        utils.indent(xml)
        etree.ElementTree(xml).write(self.playlist_path, encoding='utf-8')

    def _window_node(self, path, node_name, node_type, pms_node):
        """
        Will save this section's node to the Kodi window variables

        Uses the same conventions/logic as Emby for Kodi does
        """
        if pms_node or not self.sync_to_kodi:
            # Check: elif node_type in ('browse', 'homevideos', 'photos'):
            window_path = path
        elif self.section_type == v.PLEX_TYPE_ARTIST:
            window_path = 'ActivateWindow(Music,%s,return)' % path
        else:
            window_path = 'ActivateWindow(Videos,%s,return)' % path
        # if node_type == 'all':
        #     var = self.node
        #     utils.window('%s.index' % var,
        #                  value=path.replace('%s_all.xml' % self.section_id, ''))
        #     utils.window('%s.title' % var, value=self.name)
        # else:
        var = '%s.%s' % (self.node, node_type)
        utils.window('%s.index' % var, value=path)
        utils.window('%s.title' % var, value=node_name)
        utils.window('%s.id' % var, value=str(self.section_id))
        utils.window('%s.path' % var, value=window_path)
        utils.window('%s.type' % var, value=self.content)
        utils.window('%s.content' % var, value=path)
        utils.window('%s.artwork' % var, value=self.artwork)

    def remove_files_from_kodi(self):
        """
        Removes this sections from the Kodi userdata library folder (if appl.)
        Also removes the smart playlist
        """
        if self.section_type in (v.PLEX_TYPE_ARTIST, v.PLEX_TYPE_PHOTO):
            # No files created for these types
            return
        if path_ops.exists(self.path):
            path_ops.rmtree(self.path, ignore_errors=True)
        if path_ops.exists(self.playlist_path):
            try:
                path_ops.remove(self.playlist_path)
            except (OSError, IOError):
                LOG.warn('Could not delete smart playlist for section %s: %s',
                         self.name, self.playlist_path)

    def remove_window_vars(self):
        """
        Removes all windows variables 'Plex.nodes.<section_id>.xxx'
        """
        if self.index is not None:
            _clear_window_vars(self.index)

    def remove_from_plex(self, plexdb=None):
        """
        Removes this sections completely from the Plex DB
        """
        if plexdb:
            plexdb.remove_section(self.section_id)
        else:
            with PlexDB(lock=False) as plexdb:
                plexdb.remove_section(self.section_id)

    def remove(self):
        """
        Completely and utterly removes this section from Kodi and Plex DB
        as well as from the window variables
        """
        self.remove_files_from_kodi()
        self.remove_window_vars()
        self.remove_from_plex()


def force_full_sync():
    """
    Resets the sync timestamp for all sections to 0, thus forcing a subsequent
    full sync (not delta)
    """
    LOG.info('Telling PKC to do a full sync instead of a delta sync')
    with PlexDB() as plexdb:
        plexdb.force_full_sync()


def _save_sections_to_plex_db(sections):
    with PlexDB() as plexdb:
        for section in sections:
            section.to_plex_db(plexdb=plexdb)


def _retrieve_old_settings(sections, old_sections):
    """
    Overwrites the PKC settings for sections, grabing them from old_sections
    if a particular section is in both sections and old_sections

    Thus sets to the old values:
        section.last_sync
        section.kodi_tagid
        section.sync_to_kodi
        section.last_sync
    """
    for section in sections:
        for old_section in old_sections:
            if section == old_section:
                section.last_sync = old_section.last_sync
                section.kodi_tagid = old_section.kodi_tagid
                section.sync_to_kodi = old_section.sync_to_kodi
                section.last_sync = old_section.last_sync


def _delete_kodi_db_items(section):
    if section.section_type == v.PLEX_TYPE_MOVIE:
        kodi_context = kodi_db.KodiVideoDB
        types = ((v.PLEX_TYPE_MOVIE, itemtypes.Movie), )
    elif section.section_type == v.PLEX_TYPE_SHOW:
        kodi_context = kodi_db.KodiVideoDB
        types = ((v.PLEX_TYPE_SHOW, itemtypes.Show),
                 (v.PLEX_TYPE_SEASON, itemtypes.Season),
                 (v.PLEX_TYPE_EPISODE, itemtypes.Episode))
    elif section.section_type == v.PLEX_TYPE_ARTIST:
        kodi_context = kodi_db.KodiMusicDB
        types = ((v.PLEX_TYPE_ARTIST, itemtypes.Artist),
                 (v.PLEX_TYPE_ALBUM, itemtypes.Album),
                 (v.PLEX_TYPE_SONG, itemtypes.Song))
    for plex_type, context in types:
        while True:
            with PlexDB() as plexdb:
                plex_ids = list(plexdb.plexid_by_sectionid(section.section_id,
                                                           plex_type,
                                                           BATCH_SIZE))
                with kodi_context(texture_db=True) as kodidb:
                    typus = context(None, plexdb=plexdb, kodidb=kodidb)
                    for plex_id in plex_ids:
                        if IS_CANCELED():
                            return False
                        typus.remove(plex_id)
            if len(plex_ids) < BATCH_SIZE:
                break
    return True


def _choose_libraries(sections):
    """
    Displays a dialog for the user to select the libraries he wants synched

    Returns True if the user chose new sections, False if he aborted
    """
    import xbmcgui
    selectable_sections = []
    preselected = []
    index = 0
    for section in sections:
        if not app.SYNC.enable_music and section.section_type == v.PLEX_TYPE_ARTIST:
            LOG.info('Ignoring music section: %s', section)
            continue
        elif section.section_type == v.PLEX_TYPE_PHOTO:
            # We won't ever show Photo sections
            continue
        else:
            # Offer user the new section
            selectable_sections.append(section.name)
            # Sections have been either preselected by the user or they are new
            if section.sync_to_kodi:
                preselected.append(index)
            index += 1
    # Don't ask the user again for this PMS even if user cancel the sync dialog
    utils.settings('sections_asked_for_machine_identifier',
                   value=app.CONN.machine_identifier)
    # "Select Plex libraries to sync"
    selected_sections = xbmcgui.Dialog().multiselect(utils.lang(30524),
                                                     selectable_sections,
                                                     preselect=preselected,
                                                     useDetails=False)
    if selectable_sections is None:
        LOG.info('User chose not to select which libraries to sync')
        return False
    index = 0
    for section in sections:
        if not app.SYNC.enable_music and section.section_type == v.PLEX_TYPE_ARTIST:
            continue
        elif section.section_type == v.PLEX_TYPE_PHOTO:
            continue
        else:
            section.sync_to_kodi = index in selected_sections
            index += 1
    return True


def delete_playlists():
    """
    Clean up the playlists
    """
    path = path_ops.translate_path('special://profile/playlists/video/')
    for root, _, files in path_ops.walk(path):
        for file in files:
            if file.startswith('Plex'):
                path_ops.remove(path_ops.path.join(root, file))


def delete_nodes():
    """
    Clean up video nodes
    """
    path = path_ops.translate_path("special://profile/library/video/")
    for root, dirs, _ in path_ops.walk(path):
        for directory in dirs:
            if directory.startswith('Plex-'):
                path_ops.rmtree(path_ops.path.join(root, directory))
        break


def delete_files():
    """
    Deletes both all the Plex-xxx video node xmls as well as smart playlists
    """
    delete_nodes()
    delete_playlists()


def sync_from_pms(parent_self, pick_libraries=False):
    """
    Sync the Plex library sections.
    pick_libraries=True will prompt the user the select the libraries he
    wants to sync
    """
    global IS_CANCELED
    LOG.info('Starting synching sections from the PMS')
    IS_CANCELED = parent_self.isCanceled
    try:
        return _sync_from_pms(pick_libraries)
    finally:
        IS_CANCELED = None
        LOG.info('Done synching sections from the PMS: %s', SECTIONS)


def _sync_from_pms(pick_libraries):
    global SECTIONS
    # Re-set value in order to make sure we got the lastest user input
    app.SYNC.enable_music = utils.settings('enableMusic') == 'true'
    xml = PF.get_plex_sections()
    if xml is None:
        LOG.error("Error download PMS sections, abort")
        return False
    sections = []
    old_sections = []
    for i, xml_element in enumerate(xml.findall('Directory')):
        sections.append(Section(index=i, xml_element=xml_element))
    with PlexDB() as plexdb:
        for section_db in plexdb.all_sections():
            old_sections.append(Section(section_db_element=section_db))
    # Update our latest PMS sections with info saved in the PMS DB
    _retrieve_old_settings(sections, old_sections)
    if (app.CONN.machine_identifier != utils.settings('sections_asked_for_machine_identifier') or
            pick_libraries):
        if not pick_libraries:
            LOG.info('First time connecting to this PMS, choosing libraries')
        _choose_libraries(sections)

    # We got everything - save to Plex db in case Kodi restarts before we're
    # done here
    _save_sections_to_plex_db(sections)
    # Tweak some settings so Kodi does NOT scan the music folders
    if app.SYNC.direct_paths is True:
        # Will reboot Kodi is new library detected
        music.excludefromscan_music_folders(sections)

    # Delete all old sections that are obsolete
    # This will also delete sections whose name (or type) have changed
    for old_section in old_sections:
        for section in sections:
            if old_section == section:
                break
        else:
            if not old_section.sync_to_kodi:
                continue
            LOG.info('Deleting entire section: %s', old_section)
            # Remove all linked items
            if not _delete_kodi_db_items(old_section):
                return False
            # Remove the section itself
            old_section.remove()

    # Time to write the sections to Kodi
    for section in sections:
        section.to_kodi()
    # Counter that tells us how many sections we have - e.g. for skins and
    # listings
    utils.window('Plex.nodes.total', str(len(sections)))
    SECTIONS = sections
    return True


def _clear_window_vars(index):
    node = 'Plex.nodes.%s' % index
    utils.window('%s.title' % node, clear=True)
    utils.window('%s.type' % node, clear=True)
    utils.window('%s.content' % node, clear=True)
    utils.window('%s.path' % node, clear=True)
    utils.window('%s.id' % node, clear=True)
    utils.window('%s.addon_path' % node, clear=True)
    for kind in WINDOW_ARGS:
        node = 'Plex.nodes.%s.%s' % (index, kind)
        utils.window(node, clear=True)


def clear_window_vars():
    """
    Removes all references to sections stored in window vars 'Plex.nodes...'
    """
    number_of_nodes = int(utils.window('Plex.nodes.total') or 0)
    utils.window('Plex.nodes.total', clear=True)
    for index in range(number_of_nodes):
        _clear_window_vars(index)
