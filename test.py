import sys
import os
import mmap
import cairocffi as cairo
import wayland
from wayland.client.display import Display
from wayland.protocol import wayland
from wayland.utils import AnonymousFile
import math

import select
import time
import logging

log=logging.getLogger(__name__)

from cffi import FFI

xkb_ffi=FFI()

xkb_ffi.cdef("""
struct xkb_context;
struct xkb_keymap;
struct xkb_state;
typedef uint32_t xkb_keycode_t;
typedef uint32_t xkb_mod_mask_t;
typedef uint32_t xkb_layout_index_t;

enum xkb_context_flags {
    /** Do not apply any context flags. */
    XKB_CONTEXT_NO_FLAGS = 0,
    /** Create this context with an empty include path. */
    XKB_CONTEXT_NO_DEFAULT_INCLUDES = 1,
    /**
     * Don't take RMLVO names from the environment.
     * @since 0.3.0
     */
    XKB_CONTEXT_NO_ENVIRONMENT_NAMES = 2
};
struct xkb_context *
xkb_context_new(enum xkb_context_flags flags);

enum xkb_keymap_format {
    /** The current/classic XKB text format, as generated by xkbcomp -xkb. */
    XKB_KEYMAP_FORMAT_TEXT_V1 = 1
};
enum xkb_keymap_compile_flags {
    /** Do not apply any flags. */
    XKB_KEYMAP_COMPILE_NO_FLAGS = 0
};

struct xkb_keymap *
xkb_keymap_new_from_string(struct xkb_context *context, const char *string,
                           enum xkb_keymap_format format,
                           enum xkb_keymap_compile_flags flags);

struct xkb_state *
xkb_state_new(struct xkb_keymap *keymap);

int
xkb_state_key_get_utf8(struct xkb_state *state, xkb_keycode_t key,
                       char *buffer, size_t size);

enum xkb_state_component {
    /** Depressed modifiers, i.e. a key is physically holding them. */
    XKB_STATE_MODS_DEPRESSED = 1,
    /** Latched modifiers, i.e. will be unset after the next non-modifier
     *  key press. */
    XKB_STATE_MODS_LATCHED = 2,
    /** Locked modifiers, i.e. will be unset after the key provoking the
     *  lock has been pressed again. */
    XKB_STATE_MODS_LOCKED = 4,
    /** Effective modifiers, i.e. currently active and affect key
     *  processing (derived from the other state components).
     *  Use this unless you explictly care how the state came about. */
    XKB_STATE_MODS_EFFECTIVE = 8,
    /** Depressed layout, i.e. a key is physically holding it. */
    XKB_STATE_LAYOUT_DEPRESSED = 16,
    /** Latched layout, i.e. will be unset after the next non-modifier
     *  key press. */
    XKB_STATE_LAYOUT_LATCHED = 32,
    /** Locked layout, i.e. will be unset after the key provoking the lock
     *  has been pressed again. */
    XKB_STATE_LAYOUT_LOCKED = 64,
    /** Effective layout, i.e. currently active and affects key processing
     *  (derived from the other state components).
     *  Use this unless you explictly care how the state came about. */
    XKB_STATE_LAYOUT_EFFECTIVE = 128,
    /** LEDs (derived from the other state components). */
    XKB_STATE_LEDS = 256
};

enum xkb_state_component
xkb_state_update_mask(struct xkb_state *state,
                      xkb_mod_mask_t depressed_mods,
                      xkb_mod_mask_t latched_mods,
                      xkb_mod_mask_t locked_mods,
                      xkb_layout_index_t depressed_layout,
                      xkb_layout_index_t latched_layout,
                      xkb_layout_index_t locked_layout);

""")

xkb = xkb_ffi.dlopen("libxkbcommon.so")



shutdowncode=None

# List of future events; objects must support the nexttime attribute
# and alarm() method. nexttime should be the time at which the object
# next wants to be called, or None if the object temporarily does not
# need to be scheduled.
eventlist=[]

# List of file descriptors to watch with handlers.  Expected to be objects
# with a fileno() method that returns the appropriate fd number, and methods
# called doread(), dowrite(), etc.
rdlist=[]

# List of functions to invoke each time around the event loop.  These
# functions may do anything, including changing timeouts and drawing
# on the display.
ticklist=[]

# List of functions to invoke before calling select.  These functions
# may not change timeouts or draw on the display.  They will typically
# flush queued output.
preselectlist = []

class time_guard(object):
    def __init__(self, name, max_time):
        self._name = name
        self._max_time = max_time
    def __enter__(self):
        self._start_time = time.time()
    def __exit__(self, type, value, traceback):
        t = time.time()
        time_taken = t - self._start_time
        if time_taken > self._max_time:
            log.info("time_guard: %s took %f seconds",self._name,time_taken)

tick_time_guard = time_guard("tick",0.5)
preselect_time_guard = time_guard("preselect",0.1)
doread_time_guard = time_guard("doread",0.5)
dowrite_time_guard = time_guard("dowrite",0.5)
doexcept_time_guard = time_guard("doexcept",0.5)
alarm_time_guard = time_guard("alarm",0.5)

def eventloop():
    global shutdowncode
    while shutdowncode is None:
        for i in ticklist:
            with tick_time_guard:
                i()
        # Work out what the earliest timeout is
        timeout=None
        t=time.time()
        for i in eventlist:
            nt=i.nexttime
            i.mainloopnexttime=nt
            if nt is None: continue
            if timeout is None or (nt-t)<timeout:
                timeout=nt-t
        for i in preselectlist:
            with preselect_time_guard:
                i()
        try:
            (rd,wr,ex)=select.select(rdlist,[],[],timeout)
        except KeyboardInterrupt:
            (rd, wr, ex) = [], [], []
            shutdowncode = 1
        for i in rd:
            with doread_time_guard:
                i.doread()
        for i in wr:
            with dowrite_time_guard:
                i.dowrite()
        for i in ex:
            with doexcept_time_guard:
                i.doexcept()
        # Process any events whose time has come
        t=time.time()
        for i in eventlist:
            if not hasattr(i,'mainloopnexttime'): continue
            if i.mainloopnexttime and t>=i.mainloopnexttime:
                with alarm_time_guard:
                    i.alarm()


class Window(object):
    def __init__(self, connection, width, height, title="Window",
                 class_="quicktill"):
        self._w = connection
        if not self._w.shm_formats:
            raise RuntimeError("No suitable Shm formats available")
        self.width = width
        self.height = height
        self.surface = self._w.compositor.create_surface()
        self._w.surfaces[self.surface] = self
        self.shell_surface = self._w.shell.get_shell_surface(self.surface)
        self.shell_surface.set_toplevel()
        self.shell_surface.set_title(title)
        self.shell_surface.set_class(class_)
        self.shell_surface.dispatcher['ping'] = self._shell_surface_ping_handler
        
        wl_shm_format, cairo_shm_format = self._w.shm_formats[0]
        
        stride = cairo.ImageSurface.format_stride_for_width(
            cairo_shm_format, width)
        size = stride * height

        with AnonymousFile(size) as fd:
            self.shm_data = mmap.mmap(
                fd, size, prot=mmap.PROT_READ | mmap.PROT_WRITE,
                flags=mmap.MAP_SHARED)
            pool = self._w.shm.create_pool(fd, size)
            self.buffer = pool.create_buffer(
                0, width, height, stride, wl_shm_format)
            pool.destroy()

        self.surface.attach(self.buffer, 0, 0)
        self.surface.commit()

        self.s = cairo.ImageSurface(cairo_shm_format, width, height,
                                    data=self.shm_data, stride=stride)
    def close(self):
        self.surface.destroy()
        del self.s
    def redraw(self):
        """Copy the whole window surface to the display"""
        self.add_damage()
        self.commit()
    def add_damage(self, x=0, y=0, width=None, height=None):
        if width is None:
            width = self.width
        if height is None:
            height = self.height
        self.surface.damage(x, y, width, height)
    def commit(self):
        self.surface.commit()
    def pointer_motion(self, seat, time, x, y):
        pass
    def _shell_surface_ping_handler(self, shell_surface, serial):
        shell_surface.pong(serial)

class Seat(object):
    c_enum = wayland.interfaces['wl_seat'].enums['capability']
    def __init__(self, obj, connection, global_name):
        self.s = obj
        self._c = connection
        self.global_name = global_name
        self.name = None
        self.capabilities = 0
        self.pointer = None
        self.keyboard = None
        self.s.dispatcher['capabilities'] = self._capabilities
        self.s.dispatcher['name'] = self._name
    def removed(self):
        if self.pointer:
            self.pointer.release()
            self.pointer = None
        if self.keyboard:
            self.keyboard.release()
            # XXX Release the xkb state too!
            self.keyboard = None
        # ...that's odd, there's no request in the protocol to destroy
        # the seat proxy!  I suppose we just have to leave it lying
        # around.
    def _name(self, seat, name):
        print("Seat got name: {}".format(name))
        self.name = name
    def _capabilities(self, seat, c):
        print("Seat {} got capabilities: {}".format(self.name, c))
        self.capabilities = c
        if c & self.c_enum['pointer'] and not self.pointer:
            self.pointer = self.s.get_pointer()
            self.pointer.dispatcher['enter'] = self.pointer_enter
            self.pointer.dispatcher['leave'] = self.pointer_leave
            self.pointer.dispatcher['motion'] = self.pointer_motion
            self.pointer.silence['motion'] = True
            self.pointer.dispatcher['button'] = self.pointer_button
            self.pointer.dispatcher['axis'] = self.pointer_axis
            self.current_pointer_window = None
        if c & self.c_enum['keyboard'] and not self.keyboard:
            self.keyboard = self.s.get_keyboard()
            self.keyboard.dispatcher['keymap'] = self.keyboard_keymap
            self.keyboard.dispatcher['enter'] = self.keyboard_enter
            self.keyboard.dispatcher['leave'] = self.keyboard_leave
            self.keyboard.dispatcher['key'] = self.keyboard_key
            self.keyboard.dispatcher['modifiers'] = self.keyboard_modifiers
    def pointer_enter(self, pointer, serial, surface, surface_x, surface_y):
        print("pointer_enter {} {} {} {}".format(
            serial, surface, surface_x, surface_y))
        self.current_pointer_window = self._c.surfaces.get(surface, None)
        pointer.set_cursor(serial,None,0,0)
    def pointer_leave(self, pointer, serial, surface):
        print("pointer_leave {} {}".format(serial, surface))
        self.current_pointer_window = None
    def pointer_motion(self, pointer, time, surface_x, surface_y):
        self.current_pointer_window.pointer_motion(
            self, time, surface_x, surface_y)
    def pointer_button(self, pointer, serial, time, button, state):
        print("pointer_button {} {} {} {}".format(serial, time, button, state))
        if state == 1 and self.current_pointer_window:
            print("Seat {} starting shell surface move".format(self.name))
            self.current_pointer_window.shell_surface.move(self.s, serial)
    def pointer_axis(self, pointer, time, axis, value):
        print("pointer_axis {} {} {}".format(time, axis, value))
    def keyboard_keymap(self, keyboard, format_, fd, size):
        print("keyboard_keymap {} {} {}".format(format_, fd, size))
        xkb_keymap_data = mmap.mmap(
            fd, size, prot=mmap.PROT_READ, flags=mmap.MAP_SHARED)
        os.close(fd)
        self.keyboard_xkb_keymap = xkb.xkb_keymap_new_from_string(
            self._c.xkb_context, xkb_keymap_data[:size],
            xkb.XKB_KEYMAP_FORMAT_TEXT_V1, xkb.XKB_KEYMAP_COMPILE_NO_FLAGS)
        self.keyboard_xkb_state = xkb.xkb_state_new(self.keyboard_xkb_keymap)
        xkb_keymap_data.close()
    def keyboard_enter(self, keyboard, serial, surface, keys):
        print("keyboard_enter {} {} {}".format(serial, surface, keys))
    def keyboard_leave(self, keyboard, serial, surface):
        print("keyboard_leave {} {}".format(serial, surface))
    def keyboard_key(self, keyboard, serial, time, key, state):
        print("keyboard_key {} {} {} {}".format(serial, time, key, state))
        buf = xkb_ffi.new("char[10]")
        size = xkb.xkb_state_key_get_utf8(
            self.keyboard_xkb_state, key+8, buf, 10)
        if size > 0:
            pbuf = xkb_ffi.buffer(buf, size)
            pstring = bytes(pbuf[:])
            ustring = pstring.decode('utf8')
            print("size={}  ustring={}".format(size, repr(ustring)))
            if ustring == "q":
                global shutdowncode
                shutdowncode = 0
    def keyboard_modifiers(self, keyboard, serial, mods_depressed,
                           mods_latched, mods_locked, group):
        print("keyboard_modifiers {} {} {} {} {}".format(
            serial, mods_depressed, mods_latched, mods_locked, group))
        xkb.xkb_state_update_mask(
            self.keyboard_xkb_state, mods_depressed, mods_latched,
            mods_locked, group, 0, 0)

class WaylandConnection(object):
    def __init__(self):
        self.display = Display()
        self.display.connect()

        self.registry = self.display.get_registry()
        self.registry.dispatcher['global'] = self.registry_global_handler
        self.registry.dispatcher['global_remove'] = \
            self.registry_global_remove_handler

        self.xkb_context = xkb.xkb_context_new(xkb.XKB_CONTEXT_NO_FLAGS)

        # Dictionary mapping surface proxies to Window objects
        self.surfaces = {}

        self.compositor = None
        self.shell = None
        self.shm = None
        self.shm_formats = []
        self.seats = []

        # Bind to the globals that we're interested in. NB we won't
        # pick up things like shm_formats at this point; after we bind
        # to wl_shm we need another roundtrip before we can be sure to
        # have received them.
        self.display.roundtrip()

        if not self.compositor:
            raise RuntimeError("Compositor not found")
        if not self.shell:
            raise RuntimeError("Shell not found")
        if not self.shm:
            raise RuntimeError("Shm not found")

        # Pick up shm formats
        self.display.roundtrip()

        rdlist.append(self)
        preselectlist.append(self._preselect)
    def fileno(self):
        return self.display.get_fd()
    def disconnect(self):
        self.display.disconnect()
    def doread(self):
        self.display.recv()
        self.display.dispatch_pending()
    def _preselect(self):
        self.display.flush()
    def registry_global_handler(self, registry, name, interface, version):
        print("registry_global_handler: {} is {} v{}".format(
            name, interface, version))
        if interface == "wl_compositor":
            self.compositor = registry.bind(name, wayland.interfaces['wl_compositor'], version)
        elif interface == "wl_shell":
            self.shell = registry.bind(name, wayland.interfaces['wl_shell'], version)
        elif interface == "wl_shm":
            self.shm = registry.bind(name, wayland.interfaces['wl_shm'], version)
            self.shm.dispatcher['format'] = self.shm_format_handler
        elif interface == "wl_seat":
            self.seats.append(Seat(registry.bind(
                name, wayland.interfaces['wl_seat'], version), self, name))
    def registry_global_remove_handler(self, registry, name):
        print("registry_global_remove_handler: {} gone".format(name))
        for s in self.seats:
            if s.global_name == name:
                print("...it was a seat!  Releasing seat resources.")
                s.removed()
    def shm_format_handler(self, shm, format_):
        f = shm.interface.enums['format']
        if format_ == f.entries['argb8888'].value:
            self.shm_formats.append((format_, cairo.FORMAT_ARGB32))
        elif format_ == f.entries['xrgb8888'].value:
            self.shm_formats.append((format_, cairo.FORMAT_RGB24))
        elif format_ == f.entries['rgb565'].value:
            self.shm_formats.append((format_, cairo.FORMAT_RGB16_565))

if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)

    conn = WaylandConnection()
    w = Window(conn, 640, 480)
    ctx = cairo.Context(w.s)
    ctx.set_source_rgba(0,0,0,0)
    ctx.set_operator(cairo.OPERATOR_SOURCE)
    ctx.paint()
    ctx.set_operator(cairo.OPERATOR_OVER)
    ctx.scale(640, 480)
    pat = cairo.LinearGradient(0.0, 0.0, 0.0, 1.0)
    pat.add_color_stop_rgba(1, 0.7, 0, 0, 0.5)
    pat.add_color_stop_rgba(0, 0.9, 0.7, 0.2, 1)

    ctx.rectangle(0, 0, 1, 1)
    ctx.set_source(pat)
    ctx.fill()

    del pat

    ctx.translate(0.1, 0.1)

    ctx.move_to(0, 0)
    ctx.arc(0.2, 0.1, 0.1, -math.pi/2, 0)
    ctx.line_to(0.5, 0.1)
    ctx.curve_to(0.5, 0.2, 0.5, 0.4, 0.2, 0.8)
    ctx.close_path()

    ctx.set_source_rgb(0.3, 0.2, 0.5)
    ctx.set_line_width(0.02)
    ctx.stroke()

    del ctx

    w.s.flush()
    w.redraw()

    eventloop()

    w.close()
    conn.display.roundtrip()
    conn.disconnect()
    print("About to exit with code {}".format(shutdowncode))

    logging.shutdown()
    sys.exit(shutdowncode)
