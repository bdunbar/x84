"""
Terminal handler for x/84 bbs.  http://github.com/jquast/x84
"""
import multiprocessing
import threading
import logging
import socket
import time
import re


#pylint: disable=C0103
#        Invalid name "logger" for type constant
logger = logging.getLogger()

# global list of (TelnetClient, multiprocessing.Pipe, threading.Lock)
# this is a shared global variable across threads.
TERMINALS = list ()

def register_terminal(client, pipe, lock):
    """
    Register a (client, pipe, lock,) terminal
    """
    TERMINALS.append ((client, pipe, lock,))

def unregister_terminal(client, pipe, lock):
    """
    Unregister a (client, pipe, lock,) terminal
    """
    TERMINALS.remove ((client, pipe, lock,))

def terminals():
    """
    Returns list of (client, pipe, lock,) of all registered terminal sessions.
    """
    return TERMINALS

def start_process(pipe, origin, env):
    """
    A multiprocessing.Process target. Arguments:
        pipe: multiprocessing.Pipe
        termtype: TERM string (used to initialize curses)
        env: dictionary of client environment variables
    """
    import x84.blessings
    import x84.bbs.session
    # curses is initialized for the first time. telnet negotiation did its best
    # to determine the TERM. The default, 'unknown', is equivalent to a dumb
    # terminal.
    term = x84.blessings.Terminal (env.get('TERM', 'unknown'), IPCStream(pipe),
            int(env.get('LINES', '24')), int(env.get('COLUMNS', '80')))

    # spawn and begin a new session
    new_session = x84.bbs.session.Session (term, pipe, origin, env)

    # our root handler has dangerously forked file descriptors.
    # remove any existing handlers, and re-address our root logging handler
    # to an IPC event pipe, named 'logging'.
    root = logging.getLogger()
    for hdlr in root.handlers:
        root.removeHandler (hdlr)
    root.addHandler (x84.bbs.session.IPCLogHandler (pipe))

    try:
        new_session.run ()
    except KeyboardInterrupt:
        raise SystemExit

    logger.info('%s/%s end process', new_session.pid, new_session.handle)
    new_session.close ()
    pipe.send (('disconnect', ('process exit',)))

class IPCStream(object):
    """
    Connect blessings 'stream' to 'child' multiprocessing.Pipe
    only write(), fileno(), and close() are called by blessings.
    """
    def __init__(self, channel):
        self.channel = channel

    def write(self, ucs, encoding):
        """
        Sends unicode text to Pipe.
        """
        self.channel.send (('output', (ucs, encoding)))

    def fileno(self):
        """
        Returns pipe fileno.
        """
        return self.channel.fileno()

    def close(self):
        """
        Closes pipe.
        """
        return self.channel.close ()

def on_disconnect(client):
    """
    Discover the matching client in registry and remove it.
    """
    logger.info ('%s Disconnected', client.addrport())
    for o_client, o_pipe, o_lock in terminals():
        if client == o_client:
            unregister_terminal (o_client, o_pipe, o_lock)
            return True

def on_connect(client):
    """
    Spawn a ConnectTelnetTerminal() thread for each new connection.
    """
    logger.info ('%s Connected', client.addrport())
    thread = ConnectTelnetTerminal(client)
    thread.start ()

def on_naws(client):
    """
    On a NAWS event, check if client is yet registered in registry and send the
    pipe a refresh event. This is the same thing as ^L to the 'userland', but
    should indicate also that the window sizes are checked`.
    """
    for cpl in terminals():
        if client == cpl[0]:
            o_client, o_pipe = cpl[0], cpl[1]
            columns = int(o_client.env['COLUMNS'])
            rows = int(o_client.env['LINES'])
            o_pipe.send (('refresh', ('resize', (columns, rows),)))
            return True

class ConnectTelnetTerminal (threading.Thread):
    """
    This thread spawns long enough to
      1. set socket and telnet options
      2. ask about terminal type and size
      3. start a new session (as a sub-process)
    """
    DEBUG = False
    TIME_WAIT = 1.25
    TIME_PAUSE = 1.75
    TIME_POLL  = 0.10
    TTYPE_UNDETECTED = 'unknown'
    WINSIZE_TRICK = (
          ('vt100', ('\x1b[6n'), re.compile(chr(27) + r"\[(\d+);(\d+)R")),
          ('sun', ('\x1b[18t'), re.compile(chr(27) + r"\[8;(\d+);(\d+)t"))
    ) # see: xresize.c from X11.org


    def __init__(self, client):
        self.client = client
        threading.Thread.__init__(self)


    def _spawn_session(self):
        """
        Spawn a subprocess, avoiding GIL and forcing all shared data over a
        pipe. Previous versions of x/84 and prsv were single process,
        thread-based, and shared variables.

        All IPC communication occurs through the bi-directional pipe.  The
        server end (engine.py) polls the parent end of a pipe, while the client
        (session.py) polls the child.
        """
        parent_conn, child_conn = multiprocessing.Pipe()
        lock = threading.Lock()
        child_args = (child_conn, self.client.addrport(), self.client.env,)
        proc = multiprocessing.Process (target=start_process, args=child_args)
        proc.start ()
        register_terminal (self.client, parent_conn, lock)


    def banner(self):
        """
        This method is called after the connection is initiated.
        """
        # According to Roger Espel Llima (espel@drakkar.ens.fr), you can
        #   have your server send a sequence of control characters:
        # (0xff 0xfb 0x01) (0xff 0xfb 0x03) (0xff 0xfd 0x0f3).
        #   Which translates to:
        # (IAC WILL ECHO) (IAC WILL SUPPRESS-GO-AHEAD)
        # (IAC DO SUPPRESS-GO-AHEAD).
        time.sleep (0.25) # allow time for natural negotiation
        self.client.request_will_echo ()
        self.client.request_will_sga ()
        self.client.request_do_sga ()

        # wait for some bytes to be received, and if we get any bytes,
        # at least make sure to get some more, and then -- wait a bit!
        st_time = time.time()
        mrk_bytes = self.client.bytes_received
        while (0 == mrk_bytes or mrk_bytes == self.client.bytes_received) \
            and time.time() -st_time < (0.25):
            time.sleep (self.TIME_POLL)
        time.sleep (self.TIME_POLL *2)
        self._try_env ()
        # this will set .terminal_type if -still- undetected,
        # or otherwise overwrite it if it is detected different,
        self._try_ttype ()
        # this will set TERM to vt100 or sun if --still-- undetected,
        # this will set .rows, .columns if not LINES and COLUMNS
        self._try_naws ()
        # disable line-wrapping http://www.termsys.demon.co.uk/vtansi.htm
        self.client.send_str (bytes(chr(27) + '[7l'))
        # This denied telnetlib.py,
        #time.sleep (0.15)
        #import x84.bbs.exception
        #if 0 == self.client.bytes_received:
        #    raise x84.bbs.exception.ConnectionClosed (
        #            'telnet negotiation ignored by client')

    def run(self):
        """
        Negotiate and inquire about terminal type, telnet options, window size,
        and tcp socket options before spawning a new session.
        """
        import x84.bbs.exception
        try:
            self._set_socket_opts ()
            self.banner ()
            self._spawn_session ()
        except socket.error, err:
            logger.info ('Connection closed: %s', err)
            self.client.deactivate ()
        except x84.bbs.exception.ConnectionClosed, err:
            logger.info ('Connection closed: %s', err)
            self.client.deactivate ()

    def _timeleft(self, st_time):
        """
        Returns True when difference of current time and st_time is below
        TIME_WAIT.
        """
        return bool(time.time() -st_time < self.TIME_WAIT)


    def _set_socket_opts(self):
        """
        Set socket non-blocking and enable TCP KeepAlive.
        """
        self.client.sock.setblocking (0)
        self.client.sock.setsockopt (
                socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)


    def _try_env(self):
        """
        Try to snarf out some environment variables from unix machines.
        """
        from x84.telnet import NEW_ENVIRON, UNKNOWN
        if self.client.check_remote_option(NEW_ENVIRON) is True:
            logger.debug('environment enabled (unsolicted)')
            return
        logger.debug('request-do-env')
        self.client.request_do_env ()
        self.client.socket_send() # push
        st_time = time.time()
        while self.client.check_remote_option(NEW_ENVIRON) is UNKNOWN \
            and self._timeleft(st_time):
            time.sleep (self.TIME_POLL)
        if self.client.check_remote_option(NEW_ENVIRON) is UNKNOWN:
            logger.debug ('failed: NEW_ENVIRON')
            return


    def _try_naws(self):
        """
        Negotiate about window size (NAWS) telnet option (on).
        """
        if (self.client.env.get('LINES', None) is not None
                and self.client.env.get('COLUMNS', None) is not None):
            logger.debug ('window size: %sx%s (unsolicited)',
                    self.client.env.get('COLUMNS'),
                    self.client.env.get('LINES'),)
        self.client.request_do_naws ()
        self.client.socket_send() # push
        st_time = time.time()
        while (self.client.env.get('LINES', None) is None
                and self.client.env.get('COLUMNS', None) is None
                and self._timeleft(st_time)):
            time.sleep (self.TIME_POLL)
        if (self.client.env.get('LINES', None) is not None
                and self.client.env.get('COLUMNS', None) is not None):
            logger.info ('window size: %sx%s (negotiated)',
                    self.client.env.get('COLUMNS'),
                    self.client.env.get('LINES'))
            return
        logger.debug ('failed: negotiate about window size')

        # Try #2 ... this works for most any screen
        # send to client --> pos(999,999)
        # send to client --> report cursor position
        # read from client <-- window size
        # bonus: 'vt100' or 'sun' TERM type set, lol.
        logger.debug ('store-cu')
        self.client.send_str ('\x1b[s')
        for kind, query_seq, response_pattern in self.WINSIZE_TRICK:
            logger.debug ('move-to corner & query for %s', kind)
            self.client.send_str ('\x1b[999;999H')
            self.client.send_str (query_seq)
            self.client.socket_send() # push
            st_time = time.time()
            while self.client.idle() < self.TIME_PAUSE \
                    and self._timeleft(st_time):
                time.sleep (self.TIME_POLL)
            inp = self.client.get_input()
            self.client.send_str ('\x1b[r')
            logger.debug ('cursor restored')
            self.client.socket_send() # push
            match = response_pattern.search (inp)
            if match:
                height, width = match.groups()
                self.client.rows = int(height)
                self.client.columns = int(width)
                logger.info ('window size: %dx%d (corner-query hack)',
                        self.client.columns, self.client.rows)
                if self.client.env['TERM'] == 'unknown':
                    logger.warn ("env['TERM'] = %r by POS", kind)
                    self.client.env['TERM'] = kind
                self.client.env['LINES'] = height
                self.client.env['COLUMNS'] = width
                return

        logger.debug ('failed: negotiate about window size')
        # set to 80x24 if not detected
        self.client.columns, self.client.rows = 80, 24
        logger.debug ('window size: %dx%d (default)',
                self.client.columns, self.client.rows)
        self.client.env['LINES'] = str(self.client.rows)
        self.client.env['COLUMNS'] = str(self.client.columns)


    def _try_ttype(self):
        """
        Negotiate terminal type (TTYPE) telnet option (on).
        """
        detected = lambda: self.client.env['TERM'] != 'unknown'
        if detected():
            logger.info ('terminal type: %s (unsolicited)' %
                (self.client.env['TERM'],))
            return
        logger.debug ('request-terminal-type')
        self.client.request_ttype ()
        self.client.socket_send() # push
        st_time = time.time()
        while not detected() and self._timeleft(st_time):
            time.sleep (self.TIME_POLL)
        if detected():
            logger.info ('terminal type: %s (negotiated)' %
                (self.client.env['TERM'],))
            return
        logger.warn ('failed: terminal type not determined.')

# Deprecate; do we really want this?
#class POSHandler(threading.Thread):
#    """
#    This thread requires a client pipe, The telnet terminal is queried for its
#    cursor position, and that position is sent as 'pos' event to the child
#    pipe, otherwise a ('pos-reply', None) is sent if no cursor position is
#    reported within time_pause.
#    """
#    time_poll = 0.01
#    time_pause = 1.75
#    time_wait = 1.30
#    def __init__(self, pipe, client, lock, reply_event='pos-reply'):
#        self.pipe = pipe
#        self.client = client
#        self.lock = lock
#        self.reply_event = reply_event
#        threading.Thread.__init__ (self)
#
#    def _timeleft(self, st_time):
#        """
#        Returns True when difference of current time and t is below timeout
#        """
#        return bool(time.time() -st_time < self.time_wait)
#
#    def run(self):
#        logger.debug ('getpos?')
#        data = (None, None)
#        #pylint: disable=W0612
#        #        Unused variable 'ttype'
#        for (ttype, seq, pattern) in ConnectTelnetTerminal.WINSIZE_TRICK:
#            self.lock.acquire ()
#            self.client.send_str (seq)
#            self.client.socket_send() # push
#            st_time = time.time()
#            while self.client.idle() < self.time_pause \
#            and self._timeleft(st_time):
#                time.sleep (self.time_poll)
#            inp = self.client.get_input()
#            self.lock.release ()
#            match = pattern.search (inp)
#            logger.debug ('x %r/%d', inp, len(inp),)
#            if match:
#                row, col = match.groups()
#                try:
#                    data = (int(row)-1, int(col)-1)
#                    break
#                except ValueError:
#                    pass
#            if len(inp):
#                # holy crap, this isn't for us ;^)
#                self.pipe.send (('input', inp))
#                logger.error ('input bypass %r', inp)
#                continue
#        logger.debug ('send: %s, %r', self.reply_event, data,)
#        self.pipe.send ((self.reply_event, (data,)))
#        return