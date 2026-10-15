Feature: Durable coordination through an existing Chrome extension
  Scenario: Lost acknowledgement cannot authorize a second submit
    Given an extension bridge job may have been submitted
    When the extension bridge restarts
    Then the extension bridge denies another submit permit

  Scenario: An unknown outcome blocks the next job
    Given an extension bridge job may have been submitted
    When the extension bridge records an unknown outcome
    And the extension bridge restarts
    Then generation and upscale remain stopped

  Scenario: A Flow refusal survives restart
    Given an extension bridge job may have been submitted
    When the extension bridge observes unusual activity
    And the extension bridge restarts
    Then generation and upscale remain stopped
