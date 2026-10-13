import java.awt.BorderLayout;
import java.awt.CardLayout;
import java.awt.FlowLayout;
import java.awt.GridLayout;
import java.io.BufferedReader;
import java.io.InputStreamReader;
import javax.swing.BoxLayout;
import javax.swing.ButtonGroup;
import javax.swing.JButton;
import javax.swing.JCheckBox;
import javax.swing.JDialog;
import javax.swing.JFrame;
import javax.swing.JLabel;
import javax.swing.JMenu;
import javax.swing.JMenuBar;
import javax.swing.JMenuItem;
import javax.swing.JPanel;
import javax.swing.JRadioButton;
import javax.swing.JScrollPane;
import javax.swing.JTextField;
import javax.swing.JTree;
import javax.swing.SwingUtilities;
import javax.swing.tree.DefaultMutableTreeNode;
import javax.swing.tree.TreePath;

/**
 * Swing fixture for tests/integration/session_lockexit_drill.py (issues
 * #63 and #65).
 *
 * Gateway-shaped windows the controller drives through the real agent:
 *
 * - "IBKR Gateway" main frame with Configure → Settings.
 * - "Trader Workstation Configuration": a tree (API/Settings, Lock and
 *   Exit) and the Lock and Exit page laid out as in Gateway 10.50.1f
 *   (monitored_panels.p, data_holders.br): an "Auto restart" / "Auto
 *   logoff" radio group, ONE time label whose text Gateway switches with
 *   the mode (the restart text only when daily restart is allowed), the
 *   time field and an AM/PM pair. OK prints the saved state:
 *       SAVED|mode=<restart|logoff>|time=<HH:MM>|ampm=<AM|PM>|ro=<bool>|cid=<id>
 *   Cancel prints CANCELLED. The time is read the way Gateway reads it.
 *   OK with Auto restart newly selected first shows Gateway's Automatic
 *   Restart notice (NOTICE|Automatic Restart): a modal titled "IBKR
 *   Gateway", like the main frame, with Gateway's text. It saves only once
 *   the notice's OK is clicked (ACKNOWLEDGED); a dialog cancelled
 *   underneath it saves nothing (#69).
 * - "Existing session detected" (modal) on the SESSION command, with
 *   IBKR's text and buttons. A click prints CLICKED|<button>, then holds
 *   the event thread for LAG ms before closing, so the dialog is still
 *   listed after the agent has replied (CLICK_IN_WIN is asynchronous).
 *
 * - RO_STUCK makes OK leave Read-Only API as it was (#69: the API stayed
 *   read-only after the controller unticked it); RO_KEPT undoes that.
 *
 * Commands on stdin: SESSION, RESET, NO_DAILY, DAILY, RO_STUCK, RO_KEPT.
 * Not part of the shipped agent jar.
 */
public class SessionLockExitFixture {
    static final String LOGOFF_LABEL = "Set Auto Log Off Time (HH:MM)";
    static final String RESTART_LABEL = "Set Auto Restart Time (HH:MM)";
    static final int LAG = 1500;

    static String mode, time, ampm, clientId;
    static boolean readOnly, dailyAllowed = true, roStuck;
    static JFrame main;

    static void reset() {
        mode = "logoff";   // Gateway's default: Auto logoff at 11:45 PM
        time = "11:45";
        ampm = "PM";
        readOnly = true;
        clientId = "0";
    }

    /** Gateway 10.50.1f (jutils.d2.a): a strict "hh:mm" DateFormat
     *  parses from the front, so "08:00 PM" reads as 08:00, and the
     *  field is rewritten that way; AM/PM comes from the radios. */
    static String gatewayTime(String text) {
        java.text.SimpleDateFormat f = new java.text.SimpleDateFormat("hh:mm");
        f.setLenient(false);
        try {
            return f.format(f.parse(text.trim()));
        } catch (java.text.ParseException ex) {
            return "INVALID(" + text + ")";
        }
    }

    static void say(String s) {
        System.out.println(s);
        System.out.flush();
    }

    static void openConfig() {
        JDialog d = new JDialog(main, "Trader Workstation Configuration", false);

        DefaultMutableTreeNode root = new DefaultMutableTreeNode("Configuration");
        DefaultMutableTreeNode api = new DefaultMutableTreeNode("API");
        api.add(new DefaultMutableTreeNode("Settings"));
        root.add(api);
        root.add(new DefaultMutableTreeNode("Lock and Exit"));
        JTree tree = new JTree(root);
        tree.setRootVisible(false);

        CardLayout cards = new CardLayout();
        JPanel right = new JPanel(cards);
        right.add(new JPanel(), "none");

        // API → Settings
        JPanel apiPage = new JPanel(new GridLayout(0, 1));
        JPanel cidRow = new JPanel(new FlowLayout(FlowLayout.LEFT));
        cidRow.add(new JLabel("Master API client ID"));
        JTextField cid = new JTextField(clientId, 6);
        cidRow.add(cid);
        apiPage.add(cidRow);
        JCheckBox ro = new JCheckBox("Read-Only API", readOnly);
        apiPage.add(ro);
        right.add(apiPage, "api");

        // Lock and Exit, shaped like 10.50.1f
        JPanel lockPage = new JPanel();
        lockPage.setLayout(new BoxLayout(lockPage, BoxLayout.Y_AXIS));
        JRadioButton restart = new JRadioButton("Auto restart");
        JRadioButton logoff = new JRadioButton("Auto logoff");
        ButtonGroup modes = new ButtonGroup();
        modes.add(logoff);
        modes.add(restart);
        JPanel modeRow = new JPanel(new FlowLayout(FlowLayout.LEFT));
        modeRow.add(restart);
        modeRow.add(logoff);
        lockPage.add(modeRow);

        JPanel timePanel = new JPanel(new FlowLayout(FlowLayout.LEFT));
        JLabel timeLabel = new JLabel();
        timePanel.add(timeLabel);
        JPanel e = new JPanel(new FlowLayout(FlowLayout.LEFT));
        JTextField field = new JTextField(time, 6);
        JRadioButton am = new JRadioButton("AM");
        JRadioButton pm = new JRadioButton("PM");
        ButtonGroup meridiem = new ButtonGroup();
        meridiem.add(am);
        meridiem.add(pm);
        e.add(field);
        e.add(am);
        e.add(pm);
        timePanel.add(e);
        lockPage.add(timePanel);
        lockPage.add(new JLabel("Auto " + ("restart".equals(mode) ? "restart" : "log off")
                                + " at " + time + " " + ampm));

        Runnable relabel = () -> timeLabel.setText(
            restart.isSelected() && dailyAllowed ? RESTART_LABEL : LOGOFF_LABEL);
        // Action listeners only, as Gateway registers them: the label
        // follows a click, not a bare setSelected.
        restart.addActionListener(ev -> relabel.run());
        logoff.addActionListener(ev -> relabel.run());
        ("restart".equals(mode) ? restart : logoff).setSelected(true);
        ("AM".equals(ampm) ? am : pm).setSelected(true);
        relabel.run();
        right.add(lockPage, "lock");

        tree.addTreeSelectionListener(ev -> {
            TreePath p = ev.getNewLeadSelectionPath();
            String last = p == null ? "" : p.getLastPathComponent().toString();
            cards.show(right, "Settings".equals(last) ? "api"
                              : "Lock and Exit".equals(last) ? "lock" : "none");
        });

        JButton ok = new JButton("OK");
        ok.addActionListener(ev -> {
            if (restart.isSelected() && !"restart".equals(mode)) {
                say("NOTICE|Automatic Restart");
                showRestartNotice();          // modal: OK waits here
                if (!d.isDisplayable()) {
                    return;                   // cancelled underneath it
                }
            }
            mode = restart.isSelected() ? "restart" : "logoff";
            time = gatewayTime(field.getText());
            ampm = am.isSelected() ? "AM" : "PM";
            if (!roStuck) readOnly = ro.isSelected();
            clientId = cid.getText().trim();
            say("SAVED|mode=" + mode + "|time=" + time + "|ampm=" + ampm
                + "|ro=" + readOnly + "|cid=" + clientId);
            d.dispose();
        });
        JButton cancel = new JButton("Cancel");
        cancel.addActionListener(ev -> {
            say("CANCELLED");
            d.dispose();
        });
        JPanel buttons = new JPanel();
        buttons.add(ok);
        buttons.add(cancel);

        d.add(new JScrollPane(tree), BorderLayout.WEST);
        d.add(right, BorderLayout.CENTER);
        d.add(buttons, BorderLayout.SOUTH);
        d.pack();
        d.setVisible(true);
    }

    /** Gateway 10.50.1f: monitored_panels.p shows this from inside OK when
     *  the commit newly selects Auto restart (messages.aN.fN). Its title is
     *  the main frame's, as captured on 2026-10-12. */
    static void showRestartNotice() {
        JDialog n = new JDialog(main, "IBKR Gateway", true);
        n.add(new JLabel("<html><p>You have elected to have your trading platform restart"
                         + " automatically on a daily basis. There are some considerations"
                         + " you would need to take into account if you select the auto"
                         + " restart: please click here for more information.</p></html>"),
              BorderLayout.CENTER);
        JButton ok = new JButton("OK");
        ok.addActionListener(ev -> {
            say("ACKNOWLEDGED");
            n.dispose();
        });
        JPanel buttons = new JPanel();
        buttons.add(ok);
        n.add(buttons, BorderLayout.SOUTH);
        n.pack();
        n.setVisible(true);
    }

    static void showExistingSession() {
        JDialog d = new JDialog(main, "Existing session detected", true);
        JPanel body = new JPanel(new GridLayout(0, 1));
        body.add(new JLabel("EXISTING SESSION DETECTED"));
        body.add(new JLabel("IP 8.8.8.8 connected at 09:28:28 10/10/2026 UTC"));
        JPanel buttons = new JPanel();
        for (String text : new String[] {"Continue Login", "Cancel", "Exit Application"}) {
            JButton b = new JButton(text);
            b.addActionListener(ev -> {
                say("CLICKED|" + text);
                try {
                    Thread.sleep(LAG);   // a busy event thread: still listed
                } catch (InterruptedException ignored) {
                }
                d.dispose();
            });
            buttons.add(b);
        }
        d.add(body, BorderLayout.CENTER);
        d.add(buttons, BorderLayout.SOUTH);
        d.pack();
        d.setVisible(true);   // modal: runs a nested event loop
    }

    public static void main(String[] args) throws Exception {
        reset();
        SwingUtilities.invokeAndWait(() -> {
            main = new JFrame("IBKR Gateway");
            JMenuBar bar = new JMenuBar();
            JMenu configure = new JMenu("Configure");
            JMenuItem settings = new JMenuItem("Settings");
            settings.addActionListener(ev -> openConfig());
            configure.add(settings);
            bar.add(configure);
            main.setJMenuBar(bar);
            main.add(new JLabel("Interactive Brokers API Server"));
            main.pack();
            main.setVisible(true);
        });
        say("READY");
        BufferedReader in = new BufferedReader(new InputStreamReader(System.in));
        for (String line; (line = in.readLine()) != null; ) {
            switch (line.trim()) {
                case "SESSION": SwingUtilities.invokeLater(SessionLockExitFixture::showExistingSession); break;
                case "RESET": reset(); break;
                case "NO_DAILY": dailyAllowed = false; break;
                case "DAILY": dailyAllowed = true; break;
                case "RO_STUCK": roStuck = true; break;
                case "RO_KEPT": roStuck = false; break;
                default: break;
            }
            say("ACK|" + line.trim());
        }
        System.exit(0);
    }
}
