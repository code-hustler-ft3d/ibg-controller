import java.awt.CardLayout;
import java.awt.FlowLayout;
import java.io.BufferedReader;
import java.io.InputStreamReader;
import javax.swing.JButton;
import javax.swing.JDialog;
import javax.swing.JFrame;
import javax.swing.JLabel;
import javax.swing.JPanel;
import javax.swing.SwingUtilities;

/**
 * Swing fixture for tests/integration/late_disclaimer_drill.py (issue #81).
 *
 * - "IBKR Gateway" main frame holding two buttons that must never be
 *   clicked: "I Accept" on a card that isn't the one on screen (enabled,
 *   but not showing), and a greyed-out "Acknowledge". Both labels are on
 *   the controller's disclaimer allowlist, and the agent's LIST reports
 *   both.
 * - DISCLAIMER: a modal dialog with Gateway's "I understand and accept"
 *   button. A click closes it.
 * - STUCK: a dialog whose "Acknowledge" button doesn't close it, standing
 *   in for a matching button that keeps coming back. CLOSE removes it.
 *
 * Every click prints CLICKED|<where>|<label>. Commands on stdin:
 * DISCLAIMER, STUCK, CLOSE; each is answered with ACK|<command>.
 * The dialogs' titles aren't Gateway's: the controller matches the
 * button, not the title. Not part of the shipped agent jar.
 */
public class LateDisclaimerFixture {
    static JFrame main;
    static JDialog stuck;

    static void say(String s) {
        System.out.println(s);
        System.out.flush();
    }

    static JButton button(String label, String where) {
        JButton b = new JButton(label);
        b.addActionListener(e -> say("CLICKED|" + where + "|" + label));
        return b;
    }

    /** Don't take the keyboard when the drill runs on a desktop. */
    static void quiet(java.awt.Window w) {
        w.setFocusableWindowState(false);
        w.setAutoRequestFocus(false);
    }

    static void buildMain() {
        main = new JFrame("IBKR Gateway");
        quiet(main);
        JPanel cards = new JPanel(new CardLayout());
        JPanel shown = new JPanel(new FlowLayout());
        shown.add(new JLabel("API Server: connected"));
        JButton greyed = button("Acknowledge", "disabled");
        greyed.setEnabled(false);
        shown.add(greyed);
        JPanel hidden = new JPanel(new FlowLayout());
        hidden.add(button("I Accept", "hidden"));
        cards.add(shown, "shown");
        cards.add(hidden, "hidden");      // CardLayout shows the first card
        main.setContentPane(cards);
        main.setSize(420, 160);
        main.setVisible(true);
    }

    static void disclaimer() {
        JDialog d = new JDialog(main, "Disclaimer", true);
        quiet(d);
        JPanel p = new JPanel(new FlowLayout());
        p.add(new JLabel("This is not a brokerage account."));
        JButton accept = button("I understand and accept", "dialog");
        accept.addActionListener(e -> d.dispose());
        p.add(accept);
        d.setContentPane(p);
        d.setSize(420, 140);
        d.setVisible(true);               // modal: returns when it closes
    }

    static void stuck() {
        stuck = new JDialog(main, "Notice", false);
        quiet(stuck);
        JPanel p = new JPanel(new FlowLayout());
        p.add(button("Acknowledge", "stuck"));
        stuck.setContentPane(p);
        stuck.setSize(300, 120);
        stuck.setVisible(true);
    }

    public static void main(String[] args) throws Exception {
        SwingUtilities.invokeAndWait(LateDisclaimerFixture::buildMain);
        say("READY");
        BufferedReader in = new BufferedReader(new InputStreamReader(System.in));
        String line;
        while ((line = in.readLine()) != null) {
            String cmd = line.trim();
            switch (cmd) {
                case "DISCLAIMER":
                    SwingUtilities.invokeLater(LateDisclaimerFixture::disclaimer);
                    break;
                case "STUCK":
                    SwingUtilities.invokeAndWait(LateDisclaimerFixture::stuck);
                    break;
                case "CLOSE":
                    SwingUtilities.invokeAndWait(() -> {
                        if (stuck != null) stuck.dispose();
                    });
                    break;
                default:
                    continue;
            }
            say("ACK|" + cmd);
        }
        System.exit(0);
    }
}
