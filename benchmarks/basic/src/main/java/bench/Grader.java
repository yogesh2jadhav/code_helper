package bench;

public class Grader {
    public String grade(int score, boolean extraCredit) {
        int adjusted = score;
        if (extraCredit) {
            adjusted = adjusted + 5;
        }
        if (adjusted >= 90) {
            return "A";
        } else if (adjusted >= 75) {
            if (extraCredit) {
                return "B+";
            }
            return "B";
        } else {
            return "C";
        }
    }
}
