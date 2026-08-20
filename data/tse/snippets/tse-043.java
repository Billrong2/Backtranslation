private TransactionChange mergeUpdate(TransactionChange initial, TransactionChange change) {
    if (!(change.isCreateOrUpdate() && initial.isCreateOrUpdate())) {
      throw new IllegalStateException("Can only merge creates and updates");
    }
    if (!initial.uuid().equals(change.uuid())) {
      throw new IllegalStateException("Can only merge changes with same uuid");
    }
    TransactionChange.Builder builder = initial.toBuilder();
    if (change.parentUuid() != null) {
      builder.setParentUuid(change.parentUuid());
    }
    if (change.comment() != null) {
      builder.setComment(change.comment());
    }
    if (change.date() != null) {
      builder.setDate(change.date());
    }
    if (change.amount() != null) {
      builder.setAmount(change.amount());
    }
    if (change.label() != null) {
      builder.setLabel(change.label());
    }
    if (change.payeeName() != null) {
      builder.setPayeeName(change.payeeName());
    }
    if (change.transferAccount() != null) {
      builder.setTransferAccount(change.transferAccount());
    }
    if (change.methodLabel() != null) {
      builder.setMethodLabel(change.methodLabel());
    }
    if (change.crStatus() != null) {
      builder.setCrStatus(change.crStatus());
    }
    if (change.referenceNumber() != null) {
      builder.setReferenceNumber(change.referenceNumber());
    }
    if (change.pictureUri() != null) {
      builder.setPictureUri(change.pictureUri());
    }
    if (change.splitParts() != null) {
      builder.setSplitParts(change.splitParts());
    }
    return builder.setTimeStamp(System.currentTimeMillis()).build();
  }