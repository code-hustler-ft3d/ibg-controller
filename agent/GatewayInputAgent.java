package gateway_input;

import java.awt.Component;
import java.awt.Container;
import java.awt.Dialog;
import java.awt.Frame;
import java.awt.Window;
import java.io.BufferedReader;
import java.io.InputStreamReader;
import java.io.PrintWriter;
import java.lang.instrument.Instrumentation;
import java.net.StandardProtocolFamily;
import java.net.UnixDomainSocketAddress;
import java.nio.channels.Channels;
import java.nio.channels.ServerSocketChannel;
import java.nio.channels.SocketChannel;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.attribute.PosixFilePermission;
import java.util.ArrayList;
import java.util.EnumSet;
import java.util.List;

import javax.accessibility.AccessibleContext;
import javax.swing.AbstractButton;
import javax.swing.JCheckBox;
import javax.swing.JLabel;
import javax.swing.JToggleButton;
import javax.swing.JTree;
import javax.swing.SwingUtilities;
import javax.swing.text.JTextComponent;
import javax.swing.tree.TreeModel;
import javax.swing.tree.TreePath;

/**
 * In-JVM helper for the pyatspi2 gateway controller.
 *
 * Provides the operations that can't be done from outside the JVM.
 * Loaded via -javaagent:gateway-input-agent.jar in
 * INSTALL4J_ADD_VM_PARAMS. Listens on a Unix domain socket with a
 * line-based text protocol, single client at a time, no concurrency.
 *
 * Protocol (v0.2):
 *   PING                                  → OK pong
 *   GET_PID                               → OK <jvm_pid>
 *   SETTEXT <name> <text...>              → OK | ERR ...
 *   GETTEXT <name>                        → OK <current text>
 *   CLICK <name>                          → OK | ERR ...
 *   LIST [substring]                      → OK\n<type name>\n...\nEND
 *   WINDOWS                               → OK\n<type | title | modal>\n...END
 *   LABELS [substring]                    → OK\n[<win>] <text>\nEND
 *   WINDOW [title_substr]                 → OK\n<component tree dump>\nEND
 *   SETTEXT_IN_WIN <title>|<text>         → OK | ERR ...
 *   CLICK_IN_WIN <title>|<button>         → OK | ERR ...
 *   JTREE_SELECT_PATH <title>|<p1>/<p2>/..→ OK selected=<path> | ERR ...
 *   JCHECK <title>|<name>|<true|false>    → OK unchanged=<v> | OK changed=<v> | ERR ...
 *   SETTEXT_BY_LABEL <title>|<label>|<v>  → OK set label=<label> value=<v> | ERR ...
 *   SETTEXT_LOGIN_USER <text>             → OK | ERR ...
 *   SETTEXT_LOGIN_PASSWORD <text>         → OK | ERR ...
 *   WAIT_LOGIN_FRAME <timeout_ms>         → OK | ERR timeout | ERR invalid_timeout=...
 *
 * Component lookup is by AccessibleContext.getAccessibleName() or by
 * setText()/AbstractButton.getText(), matching what AT-SPI exposes.
 * The Python controller's selectors map 1:1 to what this agent looks
 * up.
 *
 * Threading rules borrowed from Lcstyle's ibctl agent:
 *   - SETTEXT/GETTEXT/JTREE_SELECT_PATH/SETTEXT_BY_LABEL use
 *     SwingUtilities.invokeAndWait — synchronous, no risk of opening
 *     modals in the text-input path.
 *   - CLICK / CLICK_IN_WIN use invokeLater — doClick() may open a
 *     modal dialog that blocks the EDT, which would deadlock
 *     invokeAndWait.
 *
 * External dependencies: only the JDK standard library + the Swing
 * and accessibility APIs bundled with Gateway's Zulu JRE. No
 * transitive jars.
 */
public class GatewayInputAgent {

    private static final String DEFAULT_SOCKET = "/tmp/gateway-input.sock";

    public static void premain(String agentArgs, Instrumentation inst) {
        String socketPath = (agentArgs != null && !agentArgs.isEmpty())
                ? agentArgs
                : DEFAULT_SOCKET;
        Thread t = new Thread(() -> serve(socketPath), "gateway-input-agent");
        t.setDaemon(true);
        t.start();
        System.out.println("[gateway-input-agent] listening on " + socketPath);
    }

    // ── Socket server (single-threaded, accept-handle-close loop) ──────

    private static void serve(String socketPath) {
        try {
            Path p = Path.of(socketPath);
            Files.deleteIfExists(p);
            ServerSocketChannel ssc = ServerSocketChannel.open(StandardProtocolFamily.UNIX);
            ssc.bind(UnixDomainSocketAddress.of(socketPath));
            try {
                Files.setPosixFilePermissions(p, EnumSet.of(
                        PosixFilePermission.OWNER_READ,
                        PosixFilePermission.OWNER_WRITE));
            } catch (Exception ignored) {
                // best-effort; abstract namespace doesn't have file perms anyway
            }

            while (true) {
                SocketChannel ch = ssc.accept();
                handle(ch);
            }
        } catch (Exception e) {
            System.err.println("[gateway-input-agent] server error: " + e);
            e.printStackTrace();
        }
    }

    private static void handle(SocketChannel ch) {
        try (SocketChannel sock = ch;
             BufferedReader in = new BufferedReader(
                     new InputStreamReader(Channels.newInputStream(sock)));
             PrintWriter out = new PrintWriter(Channels.newOutputStream(sock), true)) {
            String line;
            while ((line = in.readLine()) != null) {
                String response;
                try {
                    response = dispatch(line);
                } catch (Throwable t) {
                    response = "ERR " + t.getClass().getSimpleName() + ":" + safe(t.getMessage());
                }
                out.println(response);
            }
        } catch (Exception e) {
            System.err.println("[gateway-input-agent] handler error: " + e);
        }
    }

    // ── Protocol dispatch ──────────────────────────────────────────────

    private static String dispatch(String line) throws Exception {
        if (line == null || line.isEmpty()) {
            return "ERR empty_line";
        }
        int sp1 = line.indexOf(' ');
        String cmd = sp1 < 0 ? line : line.substring(0, sp1);
        String rest = sp1 < 0 ? "" : line.substring(sp1 + 1);

        switch (cmd) {
            case "PING":
                return "OK pong";
            case "GET_PID":
                // Return the JVM's OS process ID. Used by the controller in
                // dual-mode containers to distinguish "its own" Gateway JVM
                // from the other instance when both are present in the
                // AT-SPI desktop tree with the same app name.
                return "OK " + ProcessHandle.current().pid();
            case "SETTEXT": {
                int sp2 = rest.indexOf(' ');
                if (sp2 < 0) return "ERR settext_missing_text";
                String name = rest.substring(0, sp2);
                String text = rest.substring(sp2 + 1);
                return doSetText(name, text);
            }
            case "GETTEXT":
                return doGetText(rest);
            case "CLICK":
                return doClick(rest);
            case "LIST": {
                // Diagnostic: list all text components and buttons by accessible name
                return doList(rest);
            }
            case "WINDOWS": {
                // Diagnostic: list all currently-showing top-level windows by
                // class and title. Critical for spotting blocking dialogs that
                // have no text fields (e.g. existing-session-detected,
                // EULA/agreement popups, info dialogs with only an OK button).
                return doListWindows();
            }
            case "LABELS": {
                // Diagnostic: list visible JLabel text content. Lets the
                // controller read dialog message bodies (e.g. "Existing
                // session detected" vs "Wrong credentials").
                return doListLabels(rest);
            }
            case "WINDOW": {
                // Diagnostic: dump full component tree of windows whose
                // title contains <substring>. Pass empty for all windows.
                // Captures text from JLabel, JTextComponent (JTextField,
                // JTextArea, JEditorPane, JTextPane, JPasswordField),
                // AbstractButton (JButton, JToggleButton, JRadioButton),
                // plus showing/visible state and accessible name. This is
                // the way to see dialog body text that LABELS misses.
                return doDumpWindow(rest);
            }
            case "SETTEXT_IN_WIN": {
                // Type text into the first JTextComponent of the
                // first showing window whose title contains <substr>.
                // Protocol: SETTEXT_IN_WIN <title_substr>|<text>
                // Used for dialogs whose text fields have no
                // accessible name (e.g. IBKR's Second Factor
                // Authentication TOTP input field, which is a
                // bare 'u' class with no name or description).
                return doSetTextInWindow(rest);
            }
            case "CLICK_IN_WIN": {
                // Click a button by text (or accessible name) inside
                // the first showing window whose title contains <substr>.
                // Protocol: CLICK_IN_WIN <title_substr>|<button_text>
                return doClickInWindow(rest);
            }
            case "JTREE_SELECT_PATH": {
                // Navigate a JTree to a named path and select it.
                // Protocol: JTREE_SELECT_PATH <title_substr>|<p1>/<p2>/...
                // Walks the first JTree inside the first showing window
                // whose title contains <title_substr>. Matches path
                // components against node.toString() of each child at
                // each level. Expands parent nodes as it walks so leaf
                // nodes become reachable. Used to navigate the Gateway
                // Configuration dialog's ConfigurationTree
                // (e.g. "API/Settings").
                return doJTreeSelectPath(rest);
            }
            case "JCHECK": {
                // Set a toggle-style button (JCheckBox, JRadioButton,
                // JToggleButton) to a desired state.
                // Protocol: JCHECK <title_substr>|<name>|<true|false>
                return doJCheck(rest);
            }
            case "SETTEXT_BY_LABEL": {
                // Set a text value by matching against an adjacent
                // JLabel's text. Used for JSpinners and other text
                // fields that don't have an accessible name of their
                // own but sit next to a descriptive JLabel.
                // Protocol: SETTEXT_BY_LABEL <title_substr>|<label>|<value>
                return doSetTextByLabel(rest);
            }
            case "SETTEXT_LOGIN_USER": {
                // v0.4.2: role-based lookup for the Gateway login frame's
                // username field. Bypasses SETTEXT's name-based lookup
                // because the username field's accessible name mutates
                // across login attempts (after a failed attempt it can
                // become a JComboBox autocomplete editor whose JTextField
                // child has null AccessibleName). Uses Swing type info,
                // which is stable. Waits up to 10s for the field to
                // become editable (the field is temporarily disabled
                // during Gateway's "Attempt N: connecting to server"
                // retry animation).
                return doSetLoginUser(rest);
            }
            case "SETTEXT_LOGIN_PASSWORD": {
                // Symmetric to SETTEXT_LOGIN_USER. Password's accessible
                // name is currently stable, but role-based lookup future-
                // proofs against drift in newer Gateway versions.
                return doSetLoginPassword(rest);
            }
            case "WAIT_LOGIN_FRAME": {
                // v0.4.3: block until the Gateway login frame is showing
                // AND no modal dialog is overlaying it. Replaces
                // attempt_inplace_relogin's pyatspi
                // `wait_for(app, "password text")` wait, which times out
                // while Gateway's "Attempt N: connecting to server" modal
                // is up because AT-SPI filters the role. Swing's
                // isShowing() is truthful regardless of modal overlay, so
                // the JVM-side lookup sees the frame correctly.
                return doWaitLoginFrame(rest);
            }
            default:
                return "ERR unknown_command:" + cmd;
        }
    }

    // ── Component lookup ───────────────────────────────────────────────

    private static <T extends Component> List<T> collect(Container root, Class<T> type) {
        List<T> out = new ArrayList<>();
        collect0(root, type, out);
        return out;
    }

    private static <T extends Component> void collect0(Container c, Class<T> type, List<T> out) {
        for (Component child : c.getComponents()) {
            if (type.isInstance(child)) {
                out.add(type.cast(child));
            }
            if (child instanceof Container) {
                collect0((Container) child, type, out);
            }
        }
    }

    private static String accessibleName(Component c) {
        AccessibleContext ac = c.getAccessibleContext();
        if (ac == null) return null;
        return ac.getAccessibleName();
    }

    private static <T extends Component> T findByName(Class<T> type, String name) {
        for (Window w : Window.getWindows()) {
            if (!w.isShowing()) continue;
            for (T c : collect(w, type)) {
                if (name.equals(accessibleName(c))) return c;
            }
        }
        return null;
    }

    // ── Operations ─────────────────────────────────────────────────────

    private static String doSetText(String name, String text) throws Exception {
        JTextComponent field = findByName(JTextComponent.class, name);
        if (field == null) {
            return "ERR not_found type=text name=" + name;
        }
        SwingUtilities.invokeAndWait(() -> {
            field.requestFocusInWindow();
            field.setText(text);
        });
        return "OK";
    }

    private static String doGetText(String name) throws Exception {
        JTextComponent field = findByName(JTextComponent.class, name);
        if (field == null) {
            return "ERR not_found type=text name=" + name;
        }
        final String[] result = new String[1];
        SwingUtilities.invokeAndWait(() -> result[0] = field.getText());
        return "OK " + (result[0] == null ? "" : result[0]);
    }

    private static Window findWindowByTitleSubstring(String titleSubstring) {
        for (Window w : Window.getWindows()) {
            if (!w.isShowing()) continue;
            String title = "";
            if (w instanceof Frame) title = ((Frame) w).getTitle();
            else if (w instanceof Dialog) title = ((Dialog) w).getTitle();
            if (title == null) title = "";
            if (title.contains(titleSubstring)) return w;
        }
        return null;
    }

    private static String doSetTextInWindow(String rest) throws Exception {
        // Split on first '|' so window title substrings can contain spaces
        int pipe = rest.indexOf('|');
        if (pipe < 0) {
            return "ERR settext_in_win_missing_pipe";
        }
        String titleSubstr = rest.substring(0, pipe);
        String text = rest.substring(pipe + 1);

        Window target = findWindowByTitleSubstring(titleSubstr);
        if (target == null) {
            return "ERR not_found window_title_substring=" + titleSubstr;
        }

        List<JTextComponent> fields = collect(target, JTextComponent.class);
        // Filter to visible + editable fields (skip JLabel-like disabled ones)
        JTextComponent field = null;
        for (JTextComponent f : fields) {
            if (f.isShowing() && f.isEnabled() && f.isEditable()) {
                field = f;
                break;
            }
        }
        if (field == null && !fields.isEmpty()) {
            field = fields.get(0);  // fallback to first regardless
        }
        if (field == null) {
            return "ERR not_found text_component_in_window=" + titleSubstr;
        }

        final JTextComponent f = field;
        SwingUtilities.invokeAndWait(() -> {
            f.requestFocusInWindow();
            f.setText(text);
        });
        return "OK";
    }

    private static String doClickInWindow(String rest) {
        int pipe = rest.indexOf('|');
        if (pipe < 0) {
            return "ERR click_in_win_missing_pipe";
        }
        String titleSubstr = rest.substring(0, pipe);
        String buttonText = rest.substring(pipe + 1);

        Window target = findWindowByTitleSubstring(titleSubstr);
        if (target == null) {
            return "ERR not_found window_title_substring=" + titleSubstr;
        }

        for (AbstractButton b : collect(target, AbstractButton.class)) {
            if (!b.isShowing() || !b.isEnabled()) continue;
            String btnText = b.getText();
            String btnAccName = accessibleName(b);
            if (buttonText.equals(btnText) || buttonText.equals(btnAccName)) {
                SwingUtilities.invokeLater(b::doClick);
                try {
                    Thread.sleep(50);
                } catch (InterruptedException ignored) {
                }
                return "OK";
            }
        }
        return "ERR not_found button=" + buttonText + " in_window=" + titleSubstr;
    }

    private static String doClick(String name) {
        AbstractButton button = findByName(AbstractButton.class, name);
        if (button == null) {
            return "ERR not_found type=button name=" + name;
        }
        // invokeLater (NOT invokeAndWait) — doClick may open a modal dialog
        // that blocks the EDT, which would deadlock invokeAndWait.
        SwingUtilities.invokeLater(button::doClick);
        try {
            Thread.sleep(50);
        } catch (InterruptedException ignored) {
        }
        return "OK";
    }

    private static String doJTreeSelectPath(String rest) throws Exception {
        int pipe = rest.indexOf('|');
        if (pipe < 0) {
            return "ERR jtree_select_path_missing_pipe";
        }
        String titleSubstr = rest.substring(0, pipe);
        String pathStr = rest.substring(pipe + 1);
        String[] parts = pathStr.split("/");

        Window target = findWindowByTitleSubstring(titleSubstr);
        if (target == null) {
            return "ERR not_found window_title_substring=" + titleSubstr;
        }

        // Find the first JTree inside this window. Gateway's config
        // dialog has exactly one, on the left side.
        List<JTree> trees = collect(target, JTree.class);
        if (trees.isEmpty()) {
            return "ERR no_jtree_in_window=" + titleSubstr;
        }
        final JTree tree = trees.get(0);
        final TreeModel model = tree.getModel();

        // Walk the tree model by matching node.toString() against each
        // path component. Expand parent nodes as we walk so deeper
        // nodes become reachable (lazy children only appear after
        // expansion in some tree models).
        final Object[] pathObjs = new Object[parts.length + 1];
        pathObjs[0] = model.getRoot();
        final StringBuilder failLog = new StringBuilder();
        for (int level = 0; level < parts.length; level++) {
            Object parent = pathObjs[level];
            Object found = null;
            // Expand the parent on the EDT before we inspect children —
            // some tree models only populate children after expansion.
            final Object parentForExpand = parent;
            final Object[] parentPathArray = new Object[level + 1];
            System.arraycopy(pathObjs, 0, parentPathArray, 0, level + 1);
            SwingUtilities.invokeAndWait(() -> {
                tree.expandPath(new TreePath(parentPathArray));
            });
            int childCount = model.getChildCount(parent);
            for (int i = 0; i < childCount; i++) {
                Object child = model.getChild(parent, i);
                String childStr = child == null ? "" : child.toString();
                if (parts[level].equals(childStr)) {
                    found = child;
                    break;
                }
                failLog.append("[").append(level).append("]=").append(childStr).append(" ");
            }
            if (found == null) {
                return "ERR jtree_path_not_found at_level=" + level
                       + " want=" + parts[level]
                       + " saw=" + failLog.toString();
            }
            pathObjs[level + 1] = found;
        }

        final TreePath targetPath = new TreePath(pathObjs);
        SwingUtilities.invokeAndWait(() -> {
            tree.setSelectionPath(targetPath);
            tree.scrollPathToVisible(targetPath);
        });
        return "OK selected=" + pathStr;
    }

    private static String doJCheck(String rest) throws Exception {
        // Protocol: JCHECK <title_substr>|<name>|<true|false>
        int p1 = rest.indexOf('|');
        if (p1 < 0) return "ERR jcheck_missing_pipes";
        int p2 = rest.indexOf('|', p1 + 1);
        if (p2 < 0) return "ERR jcheck_missing_state";
        String titleSubstr = rest.substring(0, p1);
        String name = rest.substring(p1 + 1, p2);
        String stateStr = rest.substring(p2 + 1).trim();
        boolean desired;
        if ("true".equalsIgnoreCase(stateStr) || "yes".equalsIgnoreCase(stateStr)
                || "1".equals(stateStr) || "on".equalsIgnoreCase(stateStr)) {
            desired = true;
        } else if ("false".equalsIgnoreCase(stateStr) || "no".equalsIgnoreCase(stateStr)
                || "0".equals(stateStr) || "off".equalsIgnoreCase(stateStr)) {
            desired = false;
        } else {
            return "ERR jcheck_bad_state=" + stateStr;
        }

        Window target = findWindowByTitleSubstring(titleSubstr);
        if (target == null) {
            return "ERR not_found window_title_substring=" + titleSubstr;
        }

        // Find a toggle-style button matching the name. We accept any
        // AbstractButton that maintains a selected state (JCheckBox,
        // JRadioButton, JToggleButton, and install4j-obfuscated
        // subclasses of any of those). Match by accessible name first,
        // then by button text.
        JToggleButton box = null;
        for (JToggleButton c : collect(target, JToggleButton.class)) {
            if (!c.isShowing()) continue;
            String accName = accessibleName(c);
            String text = c.getText();
            if (name.equals(accName) || name.equals(text)) {
                box = c;
                break;
            }
        }
        if (box == null) {
            return "ERR not_found jtoggle=" + name + " in_window=" + titleSubstr;
        }
        final JToggleButton target2 = box;
        final boolean before = box.isSelected();
        if (before == desired) {
            return "OK unchanged value=" + desired;
        }
        SwingUtilities.invokeAndWait(target2::doClick);
        return "OK changed from=" + before + " to=" + desired;
    }

    private static String doSetTextByLabel(String rest) throws Exception {
        // Protocol: SETTEXT_BY_LABEL <title_substr>|<label_text>|<value>
        // Finds a JLabel whose text matches <label_text> inside the
        // first showing window whose title contains <title_substr>,
        // then walks the label's parent container and sets the first
        // JTextComponent that appears AFTER the label to <value>.
        // Used for dialogs where a spinner/text field doesn't have an
        // accessible name of its own but sits next to a descriptive
        // JLabel (e.g. "Master API client ID" in Gateway's config).
        int p1 = rest.indexOf('|');
        if (p1 < 0) return "ERR settext_by_label_missing_pipes";
        int p2 = rest.indexOf('|', p1 + 1);
        if (p2 < 0) return "ERR settext_by_label_missing_value";
        String titleSubstr = rest.substring(0, p1);
        String labelText = rest.substring(p1 + 1, p2);
        String value = rest.substring(p2 + 1);

        Window target = findWindowByTitleSubstring(titleSubstr);
        if (target == null) {
            return "ERR not_found window_title_substring=" + titleSubstr;
        }

        // Find all JLabels in the window whose text matches. There may
        // be multiple (e.g. "Master API client ID" can exist in more
        // than one tab). We pick the first one that's currently
        // showing, on the assumption that the desired tab is already
        // selected by the caller.
        JLabel matchedLabel = null;
        for (JLabel lbl : collect(target, JLabel.class)) {
            if (!lbl.isShowing()) continue;
            String t = lbl.getText();
            if (labelText.equals(t)) {
                matchedLabel = lbl;
                break;
            }
        }
        if (matchedLabel == null) {
            return "ERR label_not_found text=" + labelText + " in_window=" + titleSubstr;
        }

        // Walk up parents and look for a JTextComponent in any ancestor
        // container that's "near" the label in DFS order. The typical
        // layout is parent → [label, filler, editor, filler, help],
        // so the editor is a sibling. Start at the label's parent and
        // try there; if nothing, go one level up.
        Container parent = matchedLabel.getParent();
        JTextComponent editor = null;
        while (parent != null && editor == null) {
            boolean seenLabel = false;
            for (Component child : parent.getComponents()) {
                if (child == matchedLabel) {
                    seenLabel = true;
                    continue;
                }
                if (!seenLabel) continue;
                // Once past the label, look for an editable text
                // component (depth-first through the child subtree)
                editor = firstEditableTextIn(child);
                if (editor != null) break;
            }
            if (editor != null) break;
            parent = parent.getParent();
        }
        if (editor == null) {
            return "ERR no_editable_text_near_label text=" + labelText;
        }

        final JTextComponent f = editor;
        final String v = value;
        SwingUtilities.invokeAndWait(() -> {
            f.requestFocusInWindow();
            f.setText(v);
            // JSpinner's JFormattedTextField needs commitEdit to push
            // the typed value into the underlying model. If this isn't
            // a JFormattedTextField, the try/catch makes it a no-op.
            try {
                if (f instanceof javax.swing.JFormattedTextField) {
                    ((javax.swing.JFormattedTextField) f).commitEdit();
                }
            } catch (Exception ignored) {
            }
        });
        return "OK set label=" + labelText + " value=" + value;
    }

    private static String doSetLoginUser(String text) throws Exception {
        JTextComponent field = waitForLoginTextField(false, 10_000L);
        if (field == null) {
            return "ERR not_found editable_non_password_text in login_frame after 10s";
        }
        final JTextComponent f = field;
        SwingUtilities.invokeAndWait(() -> {
            f.requestFocusInWindow();
            f.selectAll();
            f.setText(text);
        });
        return "OK";
    }

    private static String doSetLoginPassword(String text) throws Exception {
        JTextComponent field = waitForLoginTextField(true, 10_000L);
        if (field == null) {
            return "ERR not_found editable_password in login_frame after 10s";
        }
        final JTextComponent f = field;
        SwingUtilities.invokeAndWait(() -> {
            f.requestFocusInWindow();
            f.selectAll();
            f.setText(text);
        });
        return "OK";
    }

    /**
     * Find the first showing, editable text component on the Gateway
     * login frame, polling until the deadline. The "login frame" is
     * identified by containing a JPasswordField — a stable invariant
     * of the Gateway login dialog across accessibility-name drift.
     *
     * @param wantPassword true → return the editable JPasswordField;
     *                     false → return the first editable
     *                     JTextComponent that is NOT a JPasswordField.
     */
    private static JTextComponent waitForLoginTextField(boolean wantPassword, long timeoutMs)
            throws InterruptedException {
        long deadline = System.currentTimeMillis() + timeoutMs;
        while (System.currentTimeMillis() < deadline) {
            Window loginFrame = findLoginFrame();
            if (loginFrame != null) {
                List<JTextComponent> matches = new ArrayList<>();
                for (JTextComponent f : collect(loginFrame, JTextComponent.class)) {
                    boolean isPw = f instanceof javax.swing.JPasswordField;
                    if (isPw != wantPassword) continue;
                    if (!f.isShowing() || !f.isEnabled() || !f.isEditable()) continue;
                    matches.add(f);
                }
                if (!matches.isEmpty()) {
                    if (matches.size() > 1) {
                        System.err.println("[gateway-input-agent] warning: "
                                + matches.size() + " editable "
                                + (wantPassword ? "password" : "non-password")
                                + " text components on login frame; using first");
                    }
                    return matches.get(0);
                }
            }
            Thread.sleep(100);
        }
        return null;
    }

    private static Window findLoginFrame() {
        for (Window w : Window.getWindows()) {
            if (!w.isShowing()) continue;
            if (!collect(w, javax.swing.JPasswordField.class).isEmpty()) {
                return w;
            }
        }
        return null;
    }

    private static String doWaitLoginFrame(String timeoutMsStr) throws Exception {
        long timeoutMs;
        try {
            timeoutMs = Long.parseLong(timeoutMsStr.trim());
        } catch (NumberFormatException e) {
            return "ERR invalid_timeout=" + timeoutMsStr;
        }
        long deadline = System.currentTimeMillis() + timeoutMs;
        while (System.currentTimeMillis() < deadline) {
            Window loginFrame = findLoginFrame();
            if (loginFrame != null && !modalDialogBlocking(loginFrame)) {
                return "OK";
            }
            Thread.sleep(200);
        }
        return "ERR timeout";
    }

    /**
     * True if any showing modal Dialog other than the login frame itself
     * is up — Gateway's "Attempt N: connecting to server" progress dialog
     * is one such modal, and clicking credentials into the login frame
     * while it's up is a no-op (input is routed to the modal).
     */
    private static boolean modalDialogBlocking(Window loginFrame) {
        for (Window w : Window.getWindows()) {
            if (w == loginFrame) continue;
            if (!w.isShowing()) continue;
            if (w instanceof Dialog && ((Dialog) w).isModal()) {
                return true;
            }
        }
        return false;
    }

    private static JTextComponent firstEditableTextIn(Component root) {
        if (root instanceof JTextComponent) {
            JTextComponent t = (JTextComponent) root;
            if (t.isShowing() && t.isEnabled() && t.isEditable()) return t;
        }
        if (root instanceof Container) {
            for (Component child : ((Container) root).getComponents()) {
                JTextComponent found = firstEditableTextIn(child);
                if (found != null) return found;
            }
        }
        return null;
    }

    private static String doDumpWindow(String titleFilter) {
        StringBuilder sb = new StringBuilder("OK\n");
        for (Window w : Window.getWindows()) {
            if (!w.isShowing()) continue;
            String wtitle = "";
            if (w instanceof Frame) wtitle = ((Frame) w).getTitle();
            else if (w instanceof Dialog) wtitle = ((Dialog) w).getTitle();
            if (wtitle == null) wtitle = "";
            if (!titleFilter.isEmpty() && !wtitle.contains(titleFilter)) continue;
            sb.append("=== window=").append(wtitle)
              .append(" type=").append(w.getClass().getSimpleName());
            if (w instanceof Dialog) {
                sb.append(" modal=").append(((Dialog) w).isModal());
            }
            sb.append(" ===\n");
            dumpComponentTree(w, 0, sb);
        }
        sb.append("END");
        return sb.toString();
    }

    private static void dumpComponentTree(Component c, int depth, StringBuilder sb) {
        if (depth > 30) {
            sb.append(" ".repeat(depth)).append("... (max depth)\n");
            return;
        }
        StringBuilder line = new StringBuilder();
        line.append(" ".repeat(depth)).append(c.getClass().getSimpleName());

        // Visible text from common content-bearing types
        String text = null;
        if (c instanceof AbstractButton) {
            text = ((AbstractButton) c).getText();
        } else if (c instanceof JLabel) {
            text = ((JLabel) c).getText();
        } else if (c instanceof JTextComponent) {
            text = ((JTextComponent) c).getText();
        }
        if (text != null && !text.isEmpty()) {
            String clean = text.replaceAll("<[^>]+>", " ").replaceAll("\\s+", " ").trim();
            if (clean.length() > 200) clean = clean.substring(0, 200) + "...";
            if (!clean.isEmpty()) line.append(" text=\"").append(clean).append("\"");
        }

        // Accessible name (often differs from displayed text — e.g. fields)
        AccessibleContext ac = c.getAccessibleContext();
        if (ac != null) {
            String accName = ac.getAccessibleName();
            if (accName != null && !accName.isEmpty() &&
                    (text == null || !accName.equals(text))) {
                line.append(" accName=\"").append(accName).append("\"");
            }
        }

        // States that matter for understanding dialog visibility
        if (!c.isShowing()) line.append(" hidden");
        if (!c.isEnabled()) line.append(" disabled");
        sb.append(line).append('\n');

        if (c instanceof Container) {
            for (Component child : ((Container) c).getComponents()) {
                dumpComponentTree(child, depth + 2, sb);
            }
        }
    }

    private static String doListLabels(String filter) {
        StringBuilder sb = new StringBuilder("OK\n");
        for (Window w : Window.getWindows()) {
            if (!w.isShowing()) continue;
            String wtitle = "";
            if (w instanceof Frame) wtitle = ((Frame) w).getTitle();
            else if (w instanceof Dialog) wtitle = ((Dialog) w).getTitle();
            if (wtitle == null) wtitle = "";
            for (JLabel lbl : collect(w, JLabel.class)) {
                String t = lbl.getText();
                if (t == null || t.isEmpty()) continue;
                if (!filter.isEmpty() && !t.contains(filter)) continue;
                // Strip HTML tags for readability
                String clean = t.replaceAll("<[^>]+>", " ").replaceAll("\\s+", " ").trim();
                if (clean.isEmpty()) continue;
                sb.append("[").append(wtitle).append("] ").append(clean).append('\n');
            }
        }
        sb.append("END");
        return sb.toString();
    }

    private static String doListWindows() {
        StringBuilder sb = new StringBuilder("OK\n");
        for (Window w : Window.getWindows()) {
            if (!w.isShowing()) continue;
            String type = w.getClass().getSimpleName();
            String title = "";
            if (w instanceof Frame) {
                title = ((Frame) w).getTitle();
            } else if (w instanceof Dialog) {
                title = ((Dialog) w).getTitle();
            }
            if (title == null) title = "";
            // Format: <type> | <title> | modal=<bool>
            boolean modal = false;
            if (w instanceof Dialog) {
                modal = ((Dialog) w).isModal();
            }
            sb.append(type).append(" | ").append(title).append(" | modal=").append(modal).append('\n');
        }
        sb.append("END");
        return sb.toString();
    }

    private static String doList(String filter) {
        StringBuilder sb = new StringBuilder("OK\n");
        for (Window w : Window.getWindows()) {
            if (!w.isShowing()) continue;
            for (JTextComponent t : collect(w, JTextComponent.class)) {
                String n = accessibleName(t);
                if (n == null) n = "(null)";
                if (filter.isEmpty() || n.contains(filter)) {
                    sb.append("text ").append(n).append('\n');
                }
            }
            for (AbstractButton b : collect(w, AbstractButton.class)) {
                String n = accessibleName(b);
                if (n == null) n = "(null)";
                if (filter.isEmpty() || n.contains(filter)) {
                    sb.append("button ").append(n).append('\n');
                }
            }
        }
        sb.append("END");
        return sb.toString();
    }

    private static String safe(String s) {
        if (s == null) return "";
        return s.replace('\n', ' ').replace('\r', ' ');
    }
}
