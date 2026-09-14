import java.awt.Component;
import java.awt.Frame;
import javax.swing.DefaultListCellRenderer;
import javax.swing.JDialog;
import javax.swing.JLabel;
import javax.swing.JList;
import javax.swing.JScrollPane;
import javax.swing.SwingUtilities;

/**
 * Swing fixture for tests/integration/jlist_select_drill.py (issue #33).
 *
 * Opens non-modal dialogs holding JLists shaped like Gateway's 2FA device
 * selector, and prints one line per selection change so the drill can
 * confirm JLIST_SELECT actually moved the selection rather than just
 * replying OK:
 *
 *   SELECTED|&lt;dialog title&gt;|&lt;index&gt;
 *
 * Not part of the shipped agent jar.
 */
public class JListSelectFixture {

    /** Model element whose toString() differs from what the renderer paints. */
    static final class Device {
        final String id;
        final String label;

        Device(String id, String label) {
            this.id = id;
            this.label = label;
        }

        @Override
        public String toString() {
            return id;
        }
    }

    /** Paints Device.label instead of Device.toString(). */
    static final class LabelRenderer extends DefaultListCellRenderer {
        @Override
        public Component getListCellRendererComponent(JList<?> list, Object value, int index,
                                                      boolean isSelected, boolean cellHasFocus) {
            JLabel c = (JLabel) super.getListCellRendererComponent(list, value, index, isSelected, cellHasFocus);
            if (value instanceof Device) {
                c.setText(((Device) value).label);
            }
            return c;
        }
    }

    static <E> void show(String title, JList<E> list, int initialIndex) {
        list.setSelectedIndex(initialIndex);
        list.addListSelectionListener(e -> {
            if (!e.getValueIsAdjusting()) {
                System.out.println("SELECTED|" + title + "|" + list.getSelectedIndex());
                System.out.flush();
            }
        });
        JDialog d = new JDialog((Frame) null, title, false);
        d.add(new JScrollPane(list));
        d.pack();
        d.setVisible(true);
    }

    public static void main(String[] args) throws Exception {
        SwingUtilities.invokeAndWait(() -> {
            // Shape of the real #20 selector: IB Key pre-selected.
            show("Second Factor Authentication",
                 new JList<>(new String[] {"IB Key", "Mobile Authenticator app"}), 0);

            // toString() is an internal id; only the renderer shows the label.
            JList<Device> byRenderer = new JList<>(new Device[] {
                new Device("IBKEY", "IB Key"),
                new Device("MOBILE", "Mobile Authenticator app")});
            byRenderer.setCellRenderer(new LabelRenderer());
            show("Renderer Case", byRenderer, 0);

            // Renderer paints HTML markup.
            JList<Device> html = new JList<>(new Device[] {
                new Device("IBKEY", "<html>IB Key</html>"),
                new Device("MOBILE", "<html><b>Mobile</b> Authenticator app</html>")});
            html.setCellRenderer(new LabelRenderer());
            show("Html Case", html, 0);

            // Two entries that differ only in case: must be refused, not guessed.
            show("Ambiguous Case",
                 new JList<>(new String[] {"Mobile Authenticator app", "MOBILE AUTHENTICATOR APP"}), 1);
        });
        System.out.println("READY");
        System.out.flush();
        Thread.sleep(Long.MAX_VALUE);
    }
}
