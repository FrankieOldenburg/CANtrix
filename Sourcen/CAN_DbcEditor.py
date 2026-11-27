"""Editing of Vector DBC files for the CAN Database Editor (standard library only, Python 3.6).

`DbcText` holds the text of a DBC file and changes it in place: messages, signals and environment
variables are added, changed, duplicated and deleted by patching exactly the lines and statements
concerned. Everything the viewer does not evaluate (attributes, node lists, signal groups, comments,
formatting) stays as it is. References to a changed message ID or signal name (CM_, VAL_, BA_, ...) are
changed along. The viewer parses the changed text again with `parse_dbc`, so its display always shows
what is in the file.
"""
import os
import re
from pathlib import Path
from decimal import Decimal, InvalidOperation

from CAN_Common import (DBC_ENV_RE, DBC_MESSAGE_RE, DBC_SIGNAL_RE, DBC_VALUE_PAIR_RE, ENV_TYPES, decode_text, ecu_names,
                        is_big_endian, parse_dbc, parse_dbc_environment, program_dir)

EMPTY_DBC = b'VERSION ""\n\n\nNS_ :\n\nBS_:\n\nBU_:\n\n'
NO_NODE = 'Vector__XXX'
EXTENDED_FLAG = 0x80000000
NAME_RE = re.compile(r'^[A-Za-z_]\w*$')
NUMBER_RE = re.compile(r'^[-+]?(\d+\.?\d*|\.\d+)([eE][-+]?\d+)?$')
MULTIPLEX_RE = re.compile(r'^(M|m\d+)?$')

_STRING = r'"(?:[^"\\]|\\.)*"'
_REST = r'(?:[^;"]|%s)*;' % _STRING  # up to the ';' that ends a statement (strings may contain ';')
_END = r'(?:[ \t]*\n)?'  # line break after a deleted statement

# Statements that refer to a message by its ID: (prefix, rest after the ID).
ID_STATEMENTS = (
    (r'CM_\s+BO_\s+', r'\s+%s\s*;' % _STRING),
    (r'BA_\s+%s\s+BO_\s+' % _STRING, r'\s+' + _REST),
    (r'BO_TX_BU_\s+', r'\s*:[^;]*;'),
    (r'SIG_GROUP_\s+', r'\s+' + _REST),
)
# Statements that refer to a signal by message ID and name: (prefix, rest after the name).
SIGNAL_STATEMENTS = (
    (r'CM_\s+SG_\s+', r'\s+%s\s*;' % _STRING),
    (r'VAL_\s+', r'\s+' + _REST),
    (r'BA_\s+%s\s+SG_\s+' % _STRING, r'\s+' + _REST),
    (r'SIG_VALTYPE_\s+', r'\s*:[^;]*;'),
    (r'SG_MUL_VAL_\s+', r'\s+' + _REST),
)
# Statements that refer to an environment variable by name: (prefix, rest after the name).
ENV_STATEMENTS = (
    (r'EV_\s+', r'\s*:' + _REST),
    (r'CM_\s+EV_\s+', r'\s+%s\s*;' % _STRING),
    (r'VAL_\s+', r'\s+' + _REST),
    (r'BA_\s+%s\s+EV_\s+' % _STRING, r'\s+' + _REST),
    (r'ENVVAR_DATA_\s+', r'\s*:[^;]*;'),
    (r'EV_DATA_\s+', r'\s*:[^;]*;'),
)


class DbcEditError(ValueError):
    """The requested change is not possible (message in English, shown to the user)."""


def escape_text(text):
    """Text of a comment: quotes get a backslash; backslashes are kept as they are (DBC files hardly ever
    escape them) unless they would swallow the closing quote."""
    return re.sub(r'\\(?=\\|"|$)', r'\\\\', text).replace('"', '\\"')


def value_text(text):
    """Value descriptions cannot contain quotes in a DBC file."""
    return str(text).replace('"', "'").replace('\n', ' ').strip()


def raw_id(message):
    """The ID of a parsed message as written in the file ('256' of '256 (0x100)')."""
    match = re.match(r'\s*(\d+)', str(message.get('id') or ''))
    return match.group(1) if match else ''


def parse_id(text):
    """ID typed by the user (decimal or 0x…) as written in the file. An ID above 0x7FF is an extended ID
    and gets the extended flag (bit 31), as DBC files do."""
    text = str(text).strip()
    try:
        number = int(text, 16) if text.lower().startswith('0x') else int(text)
    except ValueError:
        raise DbcEditError('The CAN ID "%s" is not a number.' % text)
    if number < 0 or number > 0x1FFFFFFF and not number & EXTENDED_FLAG or number >= 2 * EXTENDED_FLAG:
        raise DbcEditError('The CAN ID "%s" is out of range (up to 0x1FFFFFFF).' % text)
    if 0x7FF < number <= 0x1FFFFFFF:
        number |= EXTENDED_FLAG
    return str(number)


def check_name(name, what):
    if not NAME_RE.match(name or ''):
        raise DbcEditError('%s "%s" is not valid: letters, digits and "_", not starting with a digit.'
                           % (what, name or ''))


def check_number(text, what):
    if not NUMBER_RE.match(str(text).strip()):
        raise DbcEditError('%s "%s" is not a number.' % (what, text))
    return str(text).strip()


def check_whole(text, what, minimum=0):
    try:
        number = int(str(text).strip())
    except ValueError:
        raise DbcEditError('%s "%s" is not a whole number.' % (what, text))
    if number < minimum:
        raise DbcEditError('%s must be at least %d.' % (what, minimum))
    return number


def check_values(values):
    """values: list of (value, text) pairs, each value a whole number. Returns the checked pairs."""
    checked = []
    for value, text in values:
        try:
            number = int(str(value).strip())
        except ValueError:
            raise DbcEditError('The value "%s" of the value table is not a whole number.' % value)
        checked.append((str(number), value_text(text)))
    return checked


def values_statement(prefix, values):
    return 'VAL_ %s %s ;' % (prefix, ' '.join('%s "%s"' % pair for pair in values))


def split_names(text):
    return [name for name in re.split(r'[\s,]+', str(text or '')) if name and name != NO_NODE]


class DbcText(object):
    """Text of a DBC file with editing functions. `text` always uses '\\n'; on `to_bytes` the line
    ending and the character set of the original are restored."""

    def __init__(self, data):
        """data: content of the file (bytes) or its text."""
        if isinstance(data, bytes):
            self.utf8 = True
            if data.startswith(b'\xef\xbb\xbf'):
                self.bom = True
            else:
                self.bom = False
            try:
                data.decode('utf-8')
            except UnicodeDecodeError:
                self.utf8 = False
            source = decode_text(data)
        else:
            self.utf8, self.bom, source = True, False, data
        self.newline = '\r\n' if '\r\n' in source else '\n'
        self.text = source.replace('\r\n', '\n').replace('\r', '\n')

    def to_bytes(self, text=None):
        text = (self.text if text is None else text).replace('\n', self.newline)
        if self.utf8:
            return (b'\xef\xbb\xbf' if self.bom else b'') + text.encode('utf-8')
        try:
            return text.encode('cp1252')
        except UnicodeEncodeError:
            raise DbcEditError('The text contains characters that the character set of this file '
                               '(Windows-1252) cannot store.')

    # ------------------------------------------------------------ reading

    def parse(self):
        """(messages, environment) as the viewer shows them."""
        return parse_dbc(self.text, 'DBC'), parse_dbc_environment(self.text)

    def _lines(self):
        return self.text.split('\n')

    def _set_lines(self, lines):
        self.text = '\n'.join(lines)

    def _message_block(self, lines, identifier):
        """(index of the BO_ line, [indexes of its SG_ lines], index after the block)."""
        start = None
        for index, line in enumerate(lines):
            match = DBC_MESSAGE_RE.match(line)
            if match and match.group(1) == identifier:
                start = index
                break
        if start is None:
            raise DbcEditError('The message with CAN ID %s does not exist.' % identifier)
        signals, end = [], len(lines)
        for index in range(start + 1, len(lines)):
            if DBC_MESSAGE_RE.match(lines[index]):
                end = index
                break
            if DBC_SIGNAL_RE.match(lines[index]):
                signals.append(index)
        return start, signals, end

    def _signal_line(self, lines, identifier, name):
        start, signals, _ = self._message_block(lines, identifier)
        for index in signals:
            if DBC_SIGNAL_RE.match(lines[index]).group(1) == name:
                return start, index
        raise DbcEditError('The signal %s does not exist in the message with CAN ID %s.' % (name, identifier))

    def message_names(self):
        return [match.group(2) for match in
                (DBC_MESSAGE_RE.match(line) for line in self._lines()) if match]

    def message_ids(self):
        return [match.group(1) for match in
                (DBC_MESSAGE_RE.match(line) for line in self._lines()) if match]

    # ------------------------------------------------------------ statements

    def _regex(self, kinds, identifier, deleting=False):
        """Compiled patterns of the statements of kinds that refer to the message identifier (groups pre,
        id, post)."""
        return [re.compile(r'^(?P<pre>%s)(?P<id>%s)(?P<post>%s)%s' % (
            prefix, re.escape(identifier), rest, _END if deleting else ''), re.M) for prefix, rest in kinds]

    def _signal_regexes(self, identifier, name=None, deleting=False):
        found = []
        for prefix, rest in SIGNAL_STATEMENTS:
            found.append(re.compile(
                r'^(?P<pre>%s)(?P<id>%s)(?P<mid>\s+)(?P<sig>%s)(?![\w])(?P<post>%s)%s' % (
                    prefix, re.escape(identifier), re.escape(name) if name else r'[A-Za-z_]\w*', rest,
                    _END if deleting else ''), re.M))
        return found

    def _env_regexes(self, name, deleting=False):
        return [re.compile(r'^(?P<pre>%s)(?P<sig>%s)(?![\w])(?P<post>%s)%s' % (
            prefix, re.escape(name), rest, _END if deleting else ''), re.M) for prefix, rest in ENV_STATEMENTS]

    def _delete(self, regexes):
        for regex in regexes:
            self.text = regex.sub('', self.text)

    def _after_last(self, regexes):
        """Position after the line of the last statement matched by regexes, or None."""
        end = None
        for regex in regexes:
            for match in regex.finditer(self.text):
                end = match.end() if end is None else max(end, match.end())
        if end is None:
            return None
        newline = self.text.find('\n', end)
        return len(self.text) if newline < 0 else newline + 1

    def _insert(self, position, statement):
        if position is None or position >= len(self.text):
            if self.text and not self.text.endswith('\n'):
                self.text += '\n'
            self.text += statement + '\n'
        else:
            self.text = self.text[:position] + statement + '\n' + self.text[position:]

    def _upsert(self, regexes, statement, anchors=()):
        """Replace the first statement matched by regexes with statement (None: delete them). Without
        one, statement goes behind the last statement of anchors, else to the end of the file."""
        for regex in regexes:
            match = regex.search(self.text)
            if match:
                if statement is None:
                    self._delete([re.compile(regex.pattern + _END, re.M) for regex in regexes])
                else:
                    self.text = self.text[:match.start()] + statement + self.text[match.end():]
                return
        if statement is not None:
            self._insert(self._after_last(anchors), statement)

    def _comment_regex(self, keyword, *parts):
        return re.compile(r'^CM_\s+%s\s+%s\s+%s\s*;' % (keyword, r'\s+'.join(re.escape(part) for part in parts),
                                                          _STRING), re.M)

    def _rewrite(self, regexes, identifier=None, name=None, new_identifier=None, new_name=None):
        """Change identifier / name in the statements matched by regexes."""
        def change(match):
            groups = match.groupdict()
            text = groups['pre'] + (new_identifier if new_identifier is not None and 'id' in groups
                                    and groups['id'] is not None else (groups.get('id') or ''))
            if 'mid' in groups and groups['mid'] is not None:
                text += groups['mid']
            if groups.get('sig') is not None:
                text += new_name if new_name is not None else groups['sig']
            post = groups['post']
            if new_name is not None and name is not None and match.group('pre').startswith('SG_MUL_VAL_'):
                post = re.sub(r'(?<![\w])%s(?![\w])' % re.escape(name), new_name, post, count=1)  # multiplexor
            return text + post
        for regex in regexes:
            self.text = regex.sub(change, self.text)

    # ------------------------------------------------------------ nodes

    def _ensure_nodes(self, names):
        names = [name for name in names if name and name != NO_NODE]
        if not names:
            return
        match = re.search(r'^BU_[ \t]*:(.*)$', self.text, re.M)
        if match:
            known = set(match.group(1).split())
            missing = [name for name in dict.fromkeys(names) if name not in known]
            if missing:
                self.text = (self.text[:match.end()].rstrip(' \t') + ''.join(' ' + name for name in missing)
                             + self.text[match.end():])
            return
        lines = self._lines()
        first = next((index for index, line in enumerate(lines) if DBC_MESSAGE_RE.match(line)), len(lines))
        lines[first:first] = ['BU_: ' + ' '.join(dict.fromkeys(names)), '']
        self._set_lines(lines)

    # ------------------------------------------------------------ signals

    @staticmethod
    def signal_fields(line):
        """Fields of a SG_ line: name, multiplex, start_bit, length, order ('1' little endian, '0' big
        endian), sign ('+' unsigned, '-' signed), factor, offset, minimum, maximum, unit, receivers."""
        match = DBC_SIGNAL_RE.match(line)
        keys = ('name', 'multiplex', 'start_bit', 'length', 'order', 'sign', 'factor', 'offset',
                'minimum', 'maximum', 'unit', 'receivers')
        fields = dict(zip(keys, match.groups()))
        fields['multiplex'] = fields['multiplex'] or ''
        fields['receivers'] = ', '.join(split_names(fields['receivers']))
        return fields

    @staticmethod
    def signal_line(fields, indent=' '):
        receivers = ','.join(split_names(fields.get('receivers'))) or NO_NODE
        multiplex = (' ' + fields['multiplex']) if fields.get('multiplex') else ''
        return '%sSG_ %s%s : %s|%s@%s%s (%s,%s) [%s|%s] "%s" %s' % (
            indent, fields['name'], multiplex, fields['start_bit'], fields['length'], fields['order'],
            fields['sign'], fields['factor'], fields['offset'], fields['minimum'], fields['maximum'],
            fields['unit'].replace('"', "'"), receivers)

    @staticmethod
    def _check_signal(fields, message_length):
        check_name(fields['name'], 'The signal name')
        start = check_whole(fields['start_bit'], 'The start bit')
        length = check_whole(fields['length'], 'The length', 1)
        if length > 512:
            raise DbcEditError('The length must not exceed 512 bits.')
        if fields['order'] not in ('0', '1') or fields['sign'] not in ('+', '-'):
            raise DbcEditError('Byte order or signedness is invalid.')
        for key, what in (('factor', 'The factor'), ('offset', 'The offset'), ('minimum', 'The minimum'),
                          ('maximum', 'The maximum')):
            fields[key] = check_number(fields[key], what)
        if not MULTIPLEX_RE.match(fields['multiplex']):
            raise DbcEditError('Multiplexing must be empty, M or m<number> (e.g. m1).')
        if message_length:
            from CAN_Common import signal_bit_numbers
            bits = signal_bit_numbers(start, length, fields['order'] == '0')
            if not bits or min(bits) < 0 or max(bits) >= message_length * 8:
                raise DbcEditError('The signal does not fit into the %d bytes of the message.' % message_length)
        return fields

    def _message_length(self, lines, start):
        return int(DBC_MESSAGE_RE.match(lines[start]).group(3))

    def _apply_signal_texts(self, identifier, name, fields):
        """Description (CM_ SG_) and value table (VAL_) of a signal, when fields has them."""
        if 'description' in fields:
            text = fields['description'] or ''
            statement = ('CM_ SG_ %s %s "%s";' % (identifier, name, escape_text(text))) if text else None
            self._upsert([self._comment_regex('SG_', identifier, name)], statement,
                         [re.compile(r'^CM_\s+SG_\s+\d+\s+\S+\s+%s\s*;' % _STRING, re.M),
                          re.compile(r'^CM_\s+BO_\s+\d+\s+%s\s*;' % _STRING, re.M)])
        if fields.get('values') is not None:
            values = check_values(fields['values'])
            regex = re.compile(r'^VAL_\s+%s\s+%s(?![\w])%s' % (re.escape(identifier), re.escape(name), _REST), re.M)
            self._upsert([regex], values_statement('%s %s' % (identifier, name), values) if values else None,
                         [re.compile(r'^VAL_\s+\d+\s+\S+\s+%s' % _REST, re.M)])

    def add_signal(self, identifier, fields, after=None):
        """New signal in the message identifier (after the signal `after`, else at the end)."""
        lines = self._lines()
        start, signals, end = self._message_block(lines, identifier)
        fields = self._check_signal(dict(fields), self._message_length(lines, start))
        for index in signals:
            if DBC_SIGNAL_RE.match(lines[index]).group(1) == fields['name']:
                raise DbcEditError('The message already has a signal named %s.' % fields['name'])
        position = (signals[-1] if signals else start) + 1
        if after:
            for index in signals:
                if DBC_SIGNAL_RE.match(lines[index]).group(1) == after:
                    position = index + 1
        lines.insert(position, self.signal_line(fields))
        self._set_lines(lines)
        self._ensure_nodes(split_names(fields.get('receivers')))
        self._apply_signal_texts(identifier, fields['name'], fields)
        return fields['name']

    def set_signal(self, identifier, name, fields):
        """Change the signal name of message identifier: fields are the keys of `signal_fields`, and
        description and values (list of (value, text)); keys that are missing stay as they are."""
        lines = self._lines()
        start, index = self._signal_line(lines, identifier, name)
        old = self.signal_fields(lines[index])
        new = dict(old)
        new.update((key, value) for key, value in fields.items() if key in old)
        new = self._check_signal(new, self._message_length(lines, start))
        if new['name'] != name:
            _, signals, _ = self._message_block(lines, identifier)
            if any(DBC_SIGNAL_RE.match(lines[i]).group(1) == new['name'] for i in signals):
                raise DbcEditError('The message already has a signal named %s.' % new['name'])
        if new != old:  # an unchanged line stays as it is in the file
            indent = re.match(r'\s*', lines[index]).group(0)
            lines[index] = self.signal_line(new, indent)
            self._set_lines(lines)
        if new['name'] != name:
            self._rewrite(self._signal_regexes(identifier, name), new_name=new['name'])
        self._ensure_nodes(split_names(new['receivers']))
        self._apply_signal_texts(identifier, new['name'], fields)
        return new['name']

    def get_signal(self, identifier, name):
        """Fields of a signal (see `signal_fields`) for a copy."""
        lines = self._lines()
        _, index = self._signal_line(lines, identifier, name)
        return self.signal_fields(lines[index])

    def delete_signal(self, identifier, name):
        lines = self._lines()
        _, index = self._signal_line(lines, identifier, name)
        del lines[index]
        self._set_lines(lines)
        self._delete(self._signal_regexes(identifier, name, deleting=True))

    # ------------------------------------------------------------ messages

    def _check_message(self, fields, own_id=None, own_name=None):
        check_name(fields['name'], 'The message name')
        length = check_whole(fields['length'], 'The length')
        if length > 64:
            raise DbcEditError('The length must not exceed 64 bytes.')
        sender = (fields.get('sender') or NO_NODE).strip()
        if sender != NO_NODE:
            check_name(sender, 'The sender')
        if fields['id'] != own_id and fields['id'] in self.message_ids():
            raise DbcEditError('A message with CAN ID %s exists already.' % fields['id'])
        if fields['name'] != own_name and fields['name'] in self.message_names():
            raise DbcEditError('A message named %s exists already.' % fields['name'])
        return sender

    def _end_of_messages(self, lines):
        last = None
        for index, line in enumerate(lines):
            if DBC_MESSAGE_RE.match(line):
                last = index
        if last is None:
            return len(lines)
        _, _, end = self._message_block(lines, DBC_MESSAGE_RE.match(lines[last]).group(1))
        end = last + 1
        while end < len(lines) and DBC_SIGNAL_RE.match(lines[end]):
            end += 1
        return end

    def add_message(self, fields):
        """New message without signals behind the last one: id (as typed), name, length, sender,
        description."""
        fields = dict(fields)
        fields['id'] = parse_id(fields['id'])
        sender = self._check_message(fields)
        lines = self._lines()
        position = self._end_of_messages(lines)
        block = ['', 'BO_ %s %s: %d %s' % (fields['id'], fields['name'], int(fields['length']), sender)]
        if position < len(lines) and lines[position].strip() == '':
            block = block[1:]  # a blank line follows already
            lines[position:position] = block
            lines.insert(position, '')
        else:
            lines[position:position] = block + ['']
        self._set_lines(lines)
        self._ensure_nodes([sender])
        self._apply_message_texts(fields['id'], fields)
        return fields['id']

    def _apply_message_texts(self, identifier, fields):
        if 'description' in fields:
            text = fields['description'] or ''
            statement = ('CM_ BO_ %s "%s";' % (identifier, escape_text(text))) if text else None
            self._upsert([self._comment_regex('BO_', identifier)], statement,
                         [re.compile(r'^CM_\s+BO_\s+\d+\s+%s\s*;' % _STRING, re.M),
                          re.compile(r'^CM_\s+SG_\s+\d+\s+\S+\s+%s\s*;' % _STRING, re.M)])

    def set_message(self, identifier, fields):
        """Change name, id, length, sender and description (keys that are missing stay)."""
        lines = self._lines()
        start, signals, _ = self._message_block(lines, identifier)
        match = DBC_MESSAGE_RE.match(lines[start])
        old = {'id': match.group(1), 'name': match.group(2), 'length': match.group(3), 'sender': match.group(4)}
        new = dict(old)
        new.update((key, value) for key, value in fields.items() if key in old)
        if new['id'] != old['id']:
            new['id'] = parse_id(new['id'])
        sender = self._check_message(new, old['id'], old['name'])
        length = int(new['length'])
        from CAN_Common import signal_bit_numbers
        for index in signals:
            signal = self.signal_fields(lines[index])
            bits = signal_bit_numbers(int(signal['start_bit']), int(signal['length']), signal['order'] == '0')
            if bits and max(bits) >= length * 8:
                raise DbcEditError('The signal %s would not fit into %d bytes.' % (signal['name'], length))
        if new != old:
            lines[start] = 'BO_ %s %s: %d %s' % (new['id'], new['name'], length, sender)
            self._set_lines(lines)
        if new['id'] != old['id']:
            self._rewrite(self._regex(ID_STATEMENTS, old['id']), new_identifier=new['id'])
            self._rewrite(self._signal_regexes(old['id']), new_identifier=new['id'])
        self._ensure_nodes([sender])
        self._apply_message_texts(new['id'], fields)
        return new['id']

    def delete_message(self, identifier):
        lines = self._lines()
        if len(self.message_ids()) < 2:
            raise DbcEditError('A DBC file needs at least one message.')
        start, signals, _ = self._message_block(lines, identifier)
        for index in sorted(signals + [start], reverse=True):
            del lines[index]
        # one blank line less where the block stood between two blank lines
        if 0 < start < len(lines) and lines[start - 1].strip() == '' and lines[start].strip() == '':
            del lines[start]
        self._set_lines(lines)
        self._delete(self._regex(ID_STATEMENTS, identifier, deleting=True))
        self._delete(self._signal_regexes(identifier, deleting=True))

    def get_message(self, identifier):
        """Header of a message for a copy: id (as in the file), name, length, sender."""
        lines = self._lines()
        start, _, _ = self._message_block(lines, identifier)
        match = DBC_MESSAGE_RE.match(lines[start])
        return {'id': match.group(1), 'name': match.group(2), 'length': match.group(3), 'sender': match.group(4)}

    def duplicate_message(self, identifier, fields):
        """Copy of a message with its signals, comments, value tables and attributes. fields: id, name
        (and optional length, sender, description) of the copy."""
        fields = dict(fields)
        fields['id'] = parse_id(fields['id'])
        lines = self._lines()
        start, signals, _ = self._message_block(lines, identifier)
        match = DBC_MESSAGE_RE.match(lines[start])
        fields.setdefault('length', match.group(3))
        fields.setdefault('sender', match.group(4))
        sender = self._check_message(fields)
        block = ['', 'BO_ %s %s: %d %s' % (fields['id'], fields['name'], int(fields['length']), sender)]
        block += [lines[index] for index in signals]
        position = self._end_of_messages(lines)
        lines[position:position] = block
        self._set_lines(lines)
        # comment, attributes, value tables: copies behind the last statement of their kind
        kinds = [(prefix, rest, False) for prefix, rest in ID_STATEMENTS[:2]]
        kinds += [(prefix, rest, True) for prefix, rest in SIGNAL_STATEMENTS[:3]]
        for prefix, rest, with_name in kinds:
            if with_name:
                specific = re.compile(r'^(?P<pre>%s)(?P<id>%s)(?P<mid>\s+)(?P<sig>[A-Za-z_]\w*)(?P<post>%s)' % (
                    prefix, re.escape(identifier), rest), re.M)
                generic = re.compile(r'^%s\d+\s+[A-Za-z_]\w*%s' % (prefix, rest), re.M)
            else:
                specific = re.compile(r'^(?P<pre>%s)(?P<id>%s)(?P<post>%s)' % (
                    prefix, re.escape(identifier), rest), re.M)
                generic = re.compile(r'^%s\d+%s' % (prefix, rest), re.M)
            for found in list(specific.finditer(self.text)):
                groups = found.groupdict()
                statement = groups['pre'] + fields['id'] + (groups.get('mid') or '') + (groups.get('sig') or '') \
                    + groups['post']
                self._insert(self._after_last([generic]), statement)
        if 'description' in fields:
            self._apply_message_texts(fields['id'], {'description': fields['description']})
        self._ensure_nodes([sender])
        return fields['id']

    # ------------------------------------------------------------ environment variables

    @staticmethod
    def _env_statement(fields):
        nodes = ','.join(split_names(fields.get('nodes'))) or NO_NODE
        return 'EV_ %s: %s [%s|%s] "%s" %s %s DUMMY_NODE_VECTOR%s %s;' % (
            fields['name'], fields['type'], fields['minimum'], fields['maximum'],
            fields['unit'].replace('"', "'"), fields['initial'] or '0', fields['ev_id'], fields['access_code'],
            nodes)

    def _check_env(self, fields, own_name=None):
        check_name(fields['name'], 'The variable name')
        if fields['type'] not in ('0', '1', '2', '3'):
            raise DbcEditError('The type of the variable is invalid.')
        fields['minimum'] = check_number(fields['minimum'], 'The minimum')
        fields['maximum'] = check_number(fields['maximum'], 'The maximum')
        if str(fields.get('initial') or '').strip():
            fields['initial'] = check_number(fields['initial'], 'The initial value')
        else:
            fields['initial'] = '0'
        if fields['name'] != own_name and fields['name'] in parse_dbc_environment(self.text):
            raise DbcEditError('An environment variable named %s exists already.' % fields['name'])
        for node in split_names(fields.get('nodes')):
            check_name(node, 'The node')
        return fields

    def _env_current(self, name):
        for match in DBC_ENV_RE.finditer(self.text):
            if match.group(1) == name:
                kind, minimum, maximum, unit, initial, ev_id, access, nodes = match.groups()[1:]
                return {'name': name, 'type': kind, 'minimum': minimum, 'maximum': maximum, 'unit': unit,
                        'initial': initial or '0', 'ev_id': ev_id or '0', 'access_code': access or '0',
                        'nodes': ', '.join(ecu_names(nodes or ''))}
        raise DbcEditError('The environment variable %s does not exist.' % name)

    def _apply_env_texts(self, name, fields):
        if 'description' in fields:
            text = fields['description'] or ''
            statement = ('CM_ EV_ %s "%s";' % (name, escape_text(text))) if text else None
            self._upsert([self._comment_regex('EV_', name)], statement,
                         [re.compile(r'^CM_\s+EV_\s+\S+\s+%s\s*;' % _STRING, re.M),
                          re.compile(r'^CM_\s+SG_\s+\d+\s+\S+\s+%s\s*;' % _STRING, re.M)])
        if fields.get('values') is not None:
            values = check_values(fields['values'])
            regex = re.compile(r'^VAL_\s+%s(?![\w])%s' % (re.escape(name), _REST), re.M)
            self._upsert([regex], values_statement(name, values) if values else None,
                         [re.compile(r'^VAL_\s+\S+\s+%s' % _REST, re.M)])

    def add_environment(self, fields):
        """New environment variable: name, type ('0' integer, '1' float, '2' string, '3' data), minimum,
        maximum, unit, initial, nodes, access (0..3), description, values."""
        fields = self._check_env(dict(fields))
        numbers = [int(self._env_current(name)['ev_id']) for name in parse_dbc_environment(self.text)]
        fields['ev_id'] = str(max(numbers) + 1 if numbers else 1)
        fields['access_code'] = '%X' % int(fields.get('access', 0))
        position = self._after_last([re.compile(r'^EV_\s+\S+\s*:' + _REST, re.M)])
        statement = self._env_statement(fields)
        if position is None:  # the first one: behind the messages, after a blank line
            lines = self._lines()
            end = self._end_of_messages(lines)
            lines[end:end] = ['', statement]
            self._set_lines(lines)
        else:
            self._insert(position, statement)
        self._ensure_nodes(split_names(fields.get('nodes')))
        self._apply_env_texts(fields['name'], fields)
        return fields['name']

    def set_environment(self, name, fields):
        current = self._env_current(name)
        new = dict(current)
        new.update((key, value) for key, value in fields.items() if key in current)
        if 'access' in fields:  # keep the other bits of the access code
            old_code = int(current['access_code'], 16)
            new['access_code'] = '%X' % ((old_code & ~0x7FFF) | int(fields['access']))
        new = self._check_env(new, name)
        if new != current:
            self._upsert([re.compile(r'^EV_\s+%s\s*:%s' % (re.escape(name), _REST), re.M)],
                         self._env_statement(new))
        if new['name'] != name:
            self._rewrite(self._env_regexes(name)[1:], name=name, new_name=new['name'])
        self._ensure_nodes(split_names(new['nodes']))
        self._apply_env_texts(new['name'], fields)
        return new['name']

    def delete_environment(self, name):
        self._env_current(name)
        self._delete(self._env_regexes(name, deleting=True))

    def get_environment(self, name):
        """Fields of an environment variable for a copy (as `add_environment` takes them)."""
        current = self._env_current(name)
        environment = parse_dbc_environment(self.text)[name]
        return dict(current, access=int(current['access_code'], 16) & 0x7FFF,
                    description=environment['description'],
                    values=[(str(int(value)), text) for value, text in sorted(environment['values'].items())])

    def duplicate_environment(self, name, new_name):
        return self.add_environment(dict(self.get_environment(name), name=new_name))




def export_dbc(messages, environment=None):
    """Build a new DBC from the parsed database model in one pass."""
    frames, transmitters, env_lines, comments = [], [], [], []
    attributes, values, types = [], [], []
    message_ids, message_names, env_names = set(), set(), set()
    nodes = {}

    def name_for(name, used):
        base = re.sub(r'[^A-Za-z0-9_]', '_', name or '') or '_'
        if base[0].isdigit():
            base = '_' + base
        name = base[:32]
        number = 2
        while name in used:
            suffix = '_%d' % number
            name = base[:32 - len(suffix)] + suffix
            number += 1
        used.add(name)
        return name

    node_names = {NO_NODE}

    def node_list(text):
        result = []
        for name in ecu_names(text or ''):
            if name == NO_NODE:
                continue
            if name not in nodes:
                nodes[name] = name_for(name, node_names)
            result.append(nodes[name])
        return list(dict.fromkeys(result))

    def description(prefix, item):
        text = item.get('description')
        if text:
            comments.append('CM_ %s "%s";' % (prefix, escape_text(text)))

    def value_table(prefix, entries):
        pairs = {}
        for raw, label in entries:
            try:
                text = str(raw).strip()
                number = (int(text, 16) if re.match(r'^[+-]?0x', text, re.I) else Decimal(text))
                if isinstance(number, Decimal) and (not number.is_finite() or number != number.to_integral_value()):
                    continue
                integer = int(number)
            except (ValueError, InvalidOperation):
                continue
            pairs[integer] = label
        if pairs:
            values.append(values_statement(prefix, check_values(pairs.items())))

    def number_field(item, key, default):
        value = item.get(key)
        return check_number(default if value is None or not str(value).strip() else value, key)

    for message in messages:
        original_name = message.get('name', '')
        name = name_for(original_name, message_names)
        try:
            raw = str(message.get('id', ''))
            token = re.search(r'0x[0-9a-f]+', raw, re.I)
            decimal_id = re.match(r'^\s*(\d+)(?:\s|$)', raw)
            identifier = parse_id(decimal_id.group(1) if decimal_id else token.group() if token else raw)
            if message.get('extended'):
                identifier = str(int(identifier) | EXTENDED_FLAG)
            if int(identifier) & ~(EXTENDED_FLAG | 0x1FFFFFFF):
                raise DbcEditError('Invalid extended CAN ID %s.' % identifier)
            if identifier in message_ids:
                raise DbcEditError('A message with CAN ID %s exists already.' % identifier)
            message_ids.add(identifier)
            length = check_whole(message.get('length'), 'The message length')
            if length > 64:
                raise DbcEditError('The length must not exceed 64 bytes.')
        except DbcEditError as error:
            raise DbcEditError('%s: %s' % (original_name, error))

        senders = node_list(message.get('senders')) or [NO_NODE]
        receivers = node_list(message.get('receivers'))
        frames.append('BO_ %s %s: %d %s' % (identifier, name, length, senders[0]))
        if len(senders) > 1:
            transmitters.append('BO_TX_BU_ %s : %s;' % (identifier, ','.join(senders)))
        description('BO_ ' + identifier, message)
        if message.get('fd') or length > 8:
            attributes.append('BA_ "VFrameFormat" BO_ %s %d;' %
                              (identifier, 15 if int(identifier) & EXTENDED_FLAG else 14))
        elif int(identifier) & EXTENDED_FLAG:
            attributes.append('BA_ "VFrameFormat" BO_ %s 1;' % identifier)

        signal_names = set()
        for signal in message['signals']:
            signal_name = name_for(signal.get('name'), signal_names)
            try:
                fields = {
                    'name': signal_name,
                    'start_bit': str(check_whole(signal.get('start_bit'), 'The start bit')),
                    'length': str(check_whole(signal.get('length'), 'The signal length', 1)),
                    'order': '0' if is_big_endian(signal) else '1',
                    'sign': '-' if signal.get('signedness', '').lower() == 'signed' else '+',
                    'unit': value_text(signal.get('unit') or ''),
                    'multiplex': signal.get('multiplex') or '',
                    'receivers': ','.join(node_list(signal.get('receivers')) or receivers),
                }
                for key, default in (('factor', '1'), ('offset', '0'), ('minimum', '0'), ('maximum', '0')):
                    fields[key] = number_field(signal, key, default)
                if not length:
                    raise DbcEditError('A message with signals must contain at least one byte.')
                DbcText._check_signal(fields, length)
            except DbcEditError as error:
                raise DbcEditError('%s / %s: %s' % (original_name, signal.get('name'), error))
            frames.append(DbcText.signal_line(fields))
            prefix = '%s %s' % (identifier, signal_name)
            description('SG_ ' + prefix, signal)
            value_table(prefix, ((item['value'], item['name']) for item in signal.get('values') or ()))
            data_type = (signal.get('data_type') or '').upper()
            if fields['length'] == '32' and re.search(r'\b(?:FLOAT32|FLOAT|IEEE754FLOAT32)\b', data_type):
                types.append('SIG_VALTYPE_ %s : 1;' % prefix)
            elif fields['length'] == '64' and re.search(r'\b(?:FLOAT64|DOUBLE|IEEE754FLOAT64)\b', data_type):
                types.append('SIG_VALTYPE_ %s : 2;' % prefix)
        frames.append('')

    env_types = {name: code for code, name in ENV_TYPES.items()}
    for index, variable in enumerate((environment or {}).values(), 1):
        name = name_for(variable.get('name'), env_names)
        kind = env_types.get(variable.get('type'), '0')
        access, _, modifier = (variable.get('access') or '').partition(', ')
        access_code = {'unrestricted': 0, 'read': 1, 'write': 2, 'read/write': 3}.get(access, 0)
        if modifier == 'string':
            access_code |= 0x8000
        fields = {'name': name, 'type': kind, 'unit': value_text(variable.get('unit') or ''),
                  'ev_id': str(index), 'access_code': '%X' % access_code,
                  'nodes': ','.join(node_list(variable.get('nodes')))}
        for key in ('minimum', 'maximum', 'initial'):
            fields[key] = number_field(variable, key, '0')
        env_lines.append(DbcText._env_statement(fields))
        description('EV_ ' + name, variable)
        value_table(name, (variable.get('values') or {}).items())

    header = EMPTY_DBC.decode('ascii').replace('BU_:', 'BU_:' + ''.join(' ' + node for node in nodes.values()))
    if attributes:
        formats = ('"StandardCAN","ExtendedCAN","reserved","J1939PG","reserved","reserved","reserved",'
                   '"reserved","reserved","reserved","reserved","reserved","reserved","reserved",'
                   '"StandardCAN_FD","ExtendedCAN_FD"')
        attributes[:0] = ['BA_DEF_ BO_ "VFrameFormat" ENUM %s;' % formats,
                          'BA_DEF_DEF_ "VFrameFormat" "StandardCAN";']
    sections = (frames, transmitters, env_lines, comments, attributes, values, types)
    return DbcText(header + '\n\n'.join('\n'.join(section).rstrip() for section in sections if section) + '\n')



# Databases generated by CANtrix: folder in Database/ -> start of the file name
GENERATED_DATABASES = {'TRACE32': 'TRACE32-Watch', 'VectorIO': 'Vector-IO'}


def generated_category(path):
    """'TRACE32' or 'VectorIO' for a database generated by CANtrix, else None."""
    path = Path(path)
    stem = GENERATED_DATABASES.get(path.parent.name)
    if stem and path.suffix.lower() == '.dbc' and (path.stem == stem or path.stem.startswith(stem + '-')):
        return path.parent.name
    return None


def _save_generated(data, category, config_name, directory=None):
    folder = Path(directory) if directory is not None else Path(program_dir()) / 'Database' / category
    folder.mkdir(parents=True, exist_ok=True)
    stem = GENERATED_DATABASES[category]
    path = folder / ('%s-%s.dbc' % (stem, config_name) if config_name else stem + '.dbc')
    if path.is_file() and path.read_bytes() == data:
        return str(path)  # unchanged: keep the file and its time stamp
    temporary = Path(str(path) + '.part')
    try:
        temporary.write_bytes(data)
        os.replace(str(temporary), str(path))
    finally:
        if temporary.exists():
            temporary.unlink()
    return str(path)


def write_generated_dbc(fields, category, directory=None, config_name=''):
    """Save an environment-only DBC as Database/<category>/<stem>-<configuration>.dbc (replacing an older one);
    without a configuration name the file is <stem>.dbc."""
    return _save_generated(export_dbc([], fields).to_bytes(), category, config_name, directory)


def copy_generated_dbc(path, config_name, directory=None):
    """The generated database under the name of another configuration (saving under a new name), so that
    configurations do not share a file. Returns path itself if it is no generated database."""
    category = generated_category(path)
    if category is None or not os.path.isfile(path):
        return path
    return _save_generated(Path(path).read_bytes(), category, config_name, directory)
